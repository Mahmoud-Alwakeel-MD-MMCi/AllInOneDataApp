import os
import sys
import re
import json
import unicodedata
import subprocess
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, inspect, text
import matplotlib.pyplot as plt
import seaborn as sns

# Imports used behind the scenes for data quality checks (hidden from UI text).
from evidently.test_suite import TestSuite
from evidently.test_preset import NoTargetPerformanceTestPreset
from evidently import ColumnMapping

# We'll keep openai here but won't advertise it in the UI.
import openai

################################################################################
#                             GLOBAL CONFIG & SETUP                             #
################################################################################

# Fixed API key and model settings (hidden from the UI)
MODEL_NAME = "o3-mini"
openai.api_key = os.getenv("OPENAI_API_KEY")

st.set_page_config(layout="wide")  # Optional: set Streamlit to wide mode
#st.set_option("server.maxUploadSize", 10000)  # Effectively remove limit (10,000 MB)

# Session state
if "db_engine" not in st.session_state:
    st.session_state["db_engine"] = None
if "schema_info" not in st.session_state:
    st.session_state["schema_info"] = {}
if "query_df" not in st.session_state:
    st.session_state["query_df"] = pd.DataFrame()
if "max_attempts" not in st.session_state:
    st.session_state["max_attempts"] = 5

################################################################################
#                             HELPER FUNCTIONS                                 #
################################################################################

def clean_text_fences_and_unicode(raw_str: str) -> str:
    """
    Strip triple-backtick fences, normalize Unicode,
    and trim leading/trailing whitespace.
    """
    if not isinstance(raw_str, str):
        return raw_str
    normalized = unicodedata.normalize("NFKD", raw_str)
    lines = normalized.strip().splitlines()
    no_fences = [line for line in lines if not line.strip().startswith("```")]
    return "\n".join(no_fences).strip()

def create_connection(sqlite_path=None, conn_str=None):
    """
    Creates a SQLAlchemy engine from an uploaded SQLite file or connection string.
    """
    try:
        if sqlite_path:
            engine = create_engine(f"sqlite:///{sqlite_path}")
        elif conn_str:
            engine = create_engine(conn_str)
        else:
            st.error("No valid database source provided.")
            return None
        # Test it quickly:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as e:
        st.error(f"Error creating database connection: {e}")
        return None

def extract_schema(engine):
    """
    Uses SQLAlchemy Inspector to extract the DB schema details.
    """
    inspector = inspect(engine)
    schema_details = {}
    for table in inspector.get_table_names():
        columns = inspector.get_columns(table)
        pks = inspector.get_pk_constraint(table)
        fks = inspector.get_foreign_keys(table)
        schema_details[table] = {
            'description': "",
            'columns': {},
            'primary_key': pks.get('constrained_columns', []),
            'foreign_keys': []
        }
        for col in columns:
            schema_details[table]['columns'][col['name']] = {
                'type': str(col.get('type', '')),
                'description': ""
            }
        for fk in fks:
            schema_details[table]['foreign_keys'].append({
                'constrained_columns': fk['constrained_columns'],
                'referred_table': fk['referred_table'],
                'referred_columns': fk['referred_columns']
            })
    return schema_details

def execute_sql_query(engine, query_str):
    """
    Executes an SQL query. Returns (DataFrame, success_bool, message).
    - If rows are returned, success_bool=True and DataFrame is returned.
    - If no rows (DDL, etc.), success_bool=True, DF empty, with a message.
    - Otherwise success_bool=False with an error message.
    """
    try:
        with engine.connect() as conn:
            df = pd.read_sql_query(text(query_str), conn)
        return (df, True, "")
    except Exception as e:
        error_str = str(e)
        # Possibly no rows (DDL statement, etc.)
        if "This result object does not return rows" in error_str:
            try:
                with engine.connect() as conn:
                    result = conn.execute(text(query_str))
                msg = f"Statement executed. Rowcount = {result.rowcount or 0}"
                return (pd.DataFrame(), True, msg)
            except Exception as e2:
                return (pd.DataFrame(), False, str(e2))
        return (pd.DataFrame(), False, error_str)

def generate_sql_from_nl(nl_query, schema_info):
    """
    Converts a natural language request into an SQL query using model=MODEL_NAME,
    hiding the usage from the user.
    """
    prompt = f"""
You are an expert SQL assistant. The database schema is:
{json.dumps(schema_info, indent=2)}

Convert this user request into a correct SQL query:
{nl_query}

Only provide the SQL, with no extra commentary.
    """
    try:
        resp = openai.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
        )
        sql_code = resp["choices"][0]["message"]["content"]
        return clean_text_fences_and_unicode(sql_code)
    except Exception as e:
        return f"-- ERROR: {e}"

def fix_sql_with_error(nl_query, schema_info, previous_sql, error_msg):
    """
    Quietly fix a failed SQL attempt by feeding the error back to the model.
    """
    prompt = f"""
We tried to turn this user request into SQL, but got an error.

User request: {nl_query}

Schema:
{json.dumps(schema_info, indent=2)}

Previously generated SQL:
{previous_sql}

Error message:
{error_msg}

Please provide a corrected SQL query only.
    """
    try:
        resp = openai.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
        )
        sql_code = resp["choices"][0]["message"]["content"]
        return clean_text_fences_and_unicode(sql_code)
    except Exception as e:
        return previous_sql  # fallback

def silent_sql_query_flow(nl_query, engine, schema_info):
    """
    1) Generate initial SQL from user query,
    2) Try up to 5 times to fix if an error occurs,
    3) Return final DataFrame and success/fail msg (and no code is shown).
    """
    max_tries = 5
    attempt_sql = generate_sql_from_nl(nl_query, schema_info)
    for i in range(max_tries):
        df, success, msg = execute_sql_query(engine, attempt_sql)
        if success:
            return df, True, msg
        # If not successful, quietly attempt fix
        attempt_sql = fix_sql_with_error(nl_query, schema_info, attempt_sql, msg)
    # If all attempts fail, return last error
    return pd.DataFrame(), False, msg

def generate_visual_code(df, user_desc, schema_info):
    """
    Generates a Python snippet for data visualization using 'data',
    with st.pyplot(fig) etc. No print statements or extra commentary.
    """
    preview_json = df.to_json()
    prompt = f"""
You are an expert data visualization assistant.
We have a DataFrame 'data' with JSON content:
{preview_json}

Schema:
{json.dumps(schema_info, indent=2)}

Task: Create a relevant chart, based on:
{user_desc}

**Requirements**:
- Use st.write/st.markdown instead of print.
- If you create a figure, call st.pyplot(fig), not plt.show().
- Return only the Python code snippet that uses 'data'.
    """
    try:
        resp = openai.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
        )
        code = resp["choices"][0]["message"]["content"]
        return clean_text_fences_and_unicode(code)
    except Exception as e:
        return f"# Visualization generation error: {e}"

def generate_analysis_code(df, user_desc, schema_info):
    """
    Similar logic for analysis.
    """
    preview_json = df.to_json()
    prompt = f"""
You are an expert data analysis assistant.
DataFrame 'data' has JSON content:
{preview_json}

Schema:
{json.dumps(schema_info, indent=2)}

User request for analysis:
{user_desc}

**Requirements**:
- Use st.write/st.markdown for output (no print).
- If a figure is made, use st.pyplot(fig).
- Return a self-contained Python snippet that uses 'data'.
    """
    try:
        resp = openai.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
        )
        code = resp["choices"][0]["message"]["content"]
        return clean_text_fences_and_unicode(code)
    except Exception as e:
        return f"# Analysis generation error: {e}"

def run_python_snippet(python_code, df):
    """
    Execute the snippet in a local context. Return None on success or error str.
    """
    local_ctx = {"data": df, "st": st, "pd": pd, "plt": plt, "sns": sns}
    try:
        exec(python_code, globals(), local_ctx)
        return None
    except Exception as e:
        # Attempt auto-install for missing modules
        err_str = str(e)
        if "No module named" in err_str:
            mod_match = re.search(r"No module named ['\"](.+?)['\"]", err_str)
            if mod_match:
                module_needed = mod_match.group(1)
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", module_needed])
                    exec(python_code, globals(), local_ctx)
                    return None
                except Exception as e2:
                    return str(e2)
        return err_str

def silent_visualization(df, user_desc, schema_info):
    """
    Generate and run visualization code behind the scenes, no user debug shown.
    """
    max_tries = 5
    code = generate_visual_code(df, user_desc, schema_info)
    for _ in range(max_tries):
        err = run_python_snippet(code, df)
        if not err:
            return True, None  # success
        # If error, quietly fix
        fix_prompt = f"""
We tried to visualize data but got an error:
{err}

Original code:
{code}

Please fix the code. Requirements: st.write(), st.markdown(), st.pyplot(fig).
        """
        try:
            resp = openai.ChatCompletion.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": fix_prompt}],
            )
            code = clean_text_fences_and_unicode(resp["choices"][0]["message"]["content"])
        except Exception as e:
            return False, f"Visualization code fix error: {e}"
    return False, "Visualization code failed after multiple attempts."

def silent_analysis(df, user_desc, schema_info):
    """
    Generate and run analysis code behind the scenes.
    """
    max_tries = 5
    code = generate_analysis_code(df, user_desc, schema_info)
    for _ in range(max_tries):
        err = run_python_snippet(code, df)
        if not err:
            return True, None
        # If error, quietly fix
        fix_prompt = f"""
We tried to analyze data but got an error:
{err}

Original code:
{code}

Please fix the code. Requirements: st.write(), st.markdown(), st.pyplot(fig).
        """
        try:
            resp = openai.ChatCompletion.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": fix_prompt}],
            )
            code = clean_text_fences_and_unicode(resp["choices"][0]["message"]["content"])
        except Exception as e:
            return False, f"Analysis code fix error: {e}"
    return False, "Analysis code failed after multiple attempts."


################################################################################
#                      APP SECTIONS - SQL Explorer UI                           #
################################################################################

def show_database_section():
    st.header("1) Connect to a Database")
    db_file = st.file_uploader("Upload a SQLite .db file", type=["db"])
    conn_str = st.text_input("Or specify a connection string", value="")
    if st.button("Connect"):
        temp_sqlite = None
        if db_file is not None:
            temp_sqlite = f"uploaded_{db_file.name}"
            with open(temp_sqlite, "wb") as f:
                f.write(db_file.getbuffer())
        eng = create_connection(temp_sqlite, conn_str)
        if eng:
            st.session_state["db_engine"] = eng
            st.session_state["schema_info"] = extract_schema(eng)
            st.success("Database connected successfully!")


def show_sql_query_section():
    if st.session_state["db_engine"] is not None:
        st.header("2) SQL Query")
        user_request = st.text_area("Enter a question or request:", value="")
        if st.button("Run Query"):
            df, success, msg = silent_sql_query_flow(
                user_request,
                st.session_state["db_engine"],
                st.session_state["schema_info"],
            )
            if success:
                st.success("Query executed successfully.")
                st.session_state["query_df"] = df
                if not df.empty:
                    st.dataframe(df)
                elif msg:
                    st.info(msg)
            else:
                st.error("Query failed. Try a custom SQL below:")
                st.error(msg)
                # Let user do a fallback
                fallback = st.text_area("Enter a manual SQL statement:")
                if st.button("Run Manual SQL"):
                    df2, ok2, msg2 = execute_sql_query(st.session_state["db_engine"], fallback)
                    if ok2:
                        st.success("Executed successfully!")
                        if not df2.empty:
                            st.dataframe(df2)
                    else:
                        st.error(msg2)


def show_visual_analysis_section():
    # Only if we have a DataFrame from above
    if not st.session_state["query_df"].empty:
        st.header("3) Data Exploration and Analysis")

        # Always show the current query result table
        st.subheader("Query Results Table:")
        st.dataframe(st.session_state["query_df"])

        tab1, tab2 = st.tabs(["Visualize", "Analyze"])

        with tab1:
            vis_req = st.text_area("Describe the desired chart/plot:", value="Show an appropriate chart of the data.")
            if st.button("Generate Visualization"):
                ok, err = silent_visualization(
                    st.session_state["query_df"],
                    vis_req,
                    st.session_state["schema_info"]
                )
                if ok:
                    st.success("Visualization complete!")
                else:
                    st.error(err)

        with tab2:
            analysis_req = st.text_area("Describe the analysis or summary you want:", value="Provide some insights.")
            if st.button("Generate Analysis"):
                ok, err = silent_analysis(
                    st.session_state["query_df"],
                    analysis_req,
                    st.session_state["schema_info"]
                )
                if ok:
                    st.success("Analysis complete!")
                else:
                    st.error(err)


################################################################################
#         DATA QUALITY, DRIFT, & STABILITY CHECK SECTION (Hidden Library)       #
################################################################################

@st.cache_data
def load_data_evidently(file):
    if file.name.lower().endswith(".csv"):
        return pd.read_csv(file)
    else:
        return pd.read_excel(file)

def show_data_quality_check_section():
    # Larger heading
    st.markdown("<h2 style='font-size:28px;'>4) Data Quality, Drift, and Stability Check</h2>", unsafe_allow_html=True)
    st.markdown("""
    This section helps you compare a **reference dataset** against a **test dataset** 
    to evaluate data drift, stability, and overall data quality. You can:
    - Upload reference and test datasets,
    - Optionally specify and filter by a datetime column,
    - Map columns between the two datasets,
    - Generate a detailed HTML report showing potential data issues, drift, or stability concerns.
    """)

    ref_file = st.file_uploader("Upload Reference Dataset", type=["csv", "xlsx"], key="ref_file_2")
    test_file = st.file_uploader("Upload Test Dataset", type=["csv", "xlsx"], key="test_file_2")

    if ref_file and test_file:
        try:
            ref_df = load_data_evidently(ref_file)
            test_df = load_data_evidently(test_file)
            st.success("Datasets loaded successfully!")
            st.write("**Reference Columns:**", list(ref_df.columns))
            st.write("**Test Columns:**", list(test_df.columns))
        except Exception as e:
            st.error("Error loading datasets:")
            st.exception(e)
            return

        # Optional datetime column
        dt_col = st.text_input("If there's a datetime column, enter its name here:")
        # If user tries specifying a date range:
        date_range = None
        if dt_col.strip():
            if dt_col.strip() in ref_df.columns:
                try:
                    dt_series = pd.to_datetime(ref_df[dt_col], errors="coerce")
                    dt_min = dt_series.min()
                    dt_max = dt_series.max()
                    if pd.isna(dt_min) or pd.isna(dt_max):
                        st.warning("Unable to parse valid datetimes in the reference dataset.")
                    else:
                        date_range = st.date_input("Date range filter (optional)", [dt_min.date(), dt_max.date()])
                except Exception:
                    st.warning("Could not parse the datetime column properly.")
            else:
                st.warning(f"'{dt_col}' does not exist in reference dataset.")

        # Let user pick which columns from the reference to map
        st.subheader("Select Reference Columns for Mapping")
        ref_cols = sorted([c.strip() for c in ref_df.columns])
        selected = st.multiselect("Columns to map from reference dataset:", options=ref_cols, default=ref_cols)

        # Build possible mapping to test columns
        test_cols = sorted([c.strip() for c in test_df.columns])
        col_mapping = {}
        if selected:
            st.markdown("#### Mapping Setup")
            for rcol in selected:
                default = rcol if rcol in test_cols else "None"
                choice_opts = ["None"] + test_cols
                chosen = st.selectbox(
                    f"Map reference col '{rcol}' to:",
                    choice_opts,
                    index=choice_opts.index(default) if default in choice_opts else 0
                )
                if chosen != "None":
                    col_mapping[rcol] = chosen

        # Numeric inputs for thresholds
        st.subheader("Check Settings")
        stattest_threshold = st.number_input("Stattest Threshold", min_value=0.0, max_value=1.0, value=0.2, step=0.01)
        drift_share = st.number_input("Drift Share", min_value=0.0, max_value=1.0, value=0.5, step=0.01)

        if st.button("Run Data Quality, Drift, and Stability Check"):
            if not col_mapping:
                st.error("No column mapping selected. Please map at least one column.")
                return
            # Prepare data
            try:
                ref_cop = ref_df.copy()
                test_cop = test_df.copy()
                ref_cop.columns = ref_cop.columns.str.strip()
                test_cop.columns = test_cop.columns.str.strip()

                # Invert mapping to rename test columns to match reference
                invert_map = {v: k for k, v in col_mapping.items()}
                test_cop = test_cop.rename(columns=invert_map)

                # Identify common columns
                common_cols = list(set(ref_cop.columns).intersection(set(test_cop.columns)))
                if len(common_cols) == 0:
                    st.error("No common columns exist after mapping!")
                    return

                # Possibly parse datetime
                dt_used = None
                if dt_col.strip() and (dt_col in common_cols):
                    dt_used = dt_col.strip()
                    ref_cop[dt_used] = pd.to_datetime(ref_cop[dt_used], errors="coerce")
                    test_cop[dt_used] = pd.to_datetime(test_cop[dt_used], errors="coerce")

                # Filter date range if set
                if dt_used and date_range and len(date_range) == 2:
                    start_d, end_d = date_range
                    ref_cop = ref_cop[
                        (ref_cop[dt_used] >= pd.Timestamp(start_d)) &
                        (ref_cop[dt_used] <= pd.Timestamp(end_d))
                    ]
                    test_cop = test_cop[
                        (test_cop[dt_used] >= pd.Timestamp(start_d)) &
                        (test_cop[dt_used] <= pd.Timestamp(end_d))
                    ]

                # Build ColumnMapping
                numeric_cols = ref_cop[common_cols].select_dtypes(include='number').columns.tolist()
                cat_cols = [c for c in common_cols if c not in numeric_cols and c != dt_used]
                column_map = ColumnMapping(
                    numerical_features=numeric_cols,
                    categorical_features=cat_cols,
                    datetime=dt_used
                )

                ref_final = ref_cop[common_cols]
                test_final = test_cop[common_cols]

                # Run behind-the-scenes checks
                tests = [NoTargetPerformanceTestPreset(
                    stattest_threshold=stattest_threshold,
                    drift_share=drift_share
                )]
                suite = TestSuite(tests=tests)
                suite.run(reference_data=ref_final, current_data=test_final, column_mapping=column_map)

                # Save and display the HTML
                os.makedirs("reports", exist_ok=True)
                out_path = os.path.join("reports", "data_quality_drift_stability_report.html")
                suite.save_html(out_path)
                st.success("Check complete! See below:")
                st.markdown(f"[Download the full report]({out_path})", unsafe_allow_html=True)

                with open(out_path, "r", encoding="utf-8") as f:
                    report_html = f.read()
                st.components.v1.html(report_html, height=800, scrolling=True)

            except Exception as ex:
                st.error("Error running data quality check:")
                st.exception(ex)

################################################################################
#                                  MAIN APP                                     #
################################################################################

def main():
    st.title("All-in-One Data App")
    st.markdown("""
        Welcome to this multi-functional data application.
        You can:
        1. Connect to a SQL database and run queries,
        2. Visualize and analyze the resulting data,
        3. Perform a **Data Quality, Drift, and Stability Check** by comparing 
           a reference dataset against a test dataset.
    """, unsafe_allow_html=True)

    # We put each major feature into a separate tab
    tab_names = ["SQL Explorer", "Data Quality, Drift, & Stability Check"]
    tab1, tab2 = st.tabs(tab_names)

    with tab1:
        show_database_section()
        show_sql_query_section()
        show_visual_analysis_section()

    with tab2:
        show_data_quality_check_section()

if __name__ == "__main__":
    main()
