import os
import re
import glob
import shutil
import tempfile
import urllib.request
import numpy as np
import pandas as pd
import streamlit as st
import jaydebeapi


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="MDB Table CSV Replacer",
    page_icon="🗂️",
    layout="wide"
)

st.title("🗂️ MDB Table CSV Replacer")

st.warning(
    "Always keep a backup of your original MDB file. "
    "This app modifies a copy of the uploaded MDB and allows you to download the updated file."
)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def quote_access_name(name):
    return "[" + str(name).replace("]", "]]") + "]"


def safe_filename(name):
    name = str(name)
    name = re.sub(r"[^A-Za-z0-9_\-\.]+", "_", name)
    return name


def get_ucanaccess_jars():
    """
    Uses existing UCanAccess JAR from lib/.
    If not found, automatically downloads UCanAccess uber JAR.
    """

    os.makedirs("lib", exist_ok=True)

    jars = glob.glob("lib/*.jar")

    if len(jars) > 0:
        return jars

    st.info("UCanAccess JAR not found. Downloading automatically...")

    ucanaccess_url = (
        "https://repo1.maven.org/maven2/io/github/spannm/"
        "ucanaccess/5.1.5/ucanaccess-5.1.5-uber.jar"
    )

    jar_path = "lib/ucanaccess-5.1.5-uber.jar"

    try:
        urllib.request.urlretrieve(ucanaccess_url, jar_path)
    except Exception as e:
        st.error(f"Could not download UCanAccess JAR: {e}")
        st.stop()

    return [jar_path]


def connect_access_db(mdb_path):
    jars = get_ucanaccess_jars()

    jdbc_url = f"jdbc:ucanaccess://{mdb_path};memory=false;showSchema=true"

    conn = jaydebeapi.connect(
        "net.ucanaccess.jdbc.UcanaccessDriver",
        jdbc_url,
        ["", ""],
        jars
    )

    return conn


def list_tables(conn):
    tables = []

    meta = conn.jconn.getMetaData()
    rs = meta.getTables(None, None, "%", None)

    while rs.next():
        table_name = rs.getString("TABLE_NAME")
        table_type = rs.getString("TABLE_TYPE")

        if table_type and table_type.upper() == "TABLE":
            if not table_name.startswith("MSys"):
                tables.append(table_name)

    rs.close()

    return sorted(tables)


def read_table(conn, table_name, limit=None):
    cur = conn.cursor()

    if limit is None:
        sql = f"SELECT * FROM {quote_access_name(table_name)}"
    else:
        sql = f"SELECT TOP {int(limit)} * FROM {quote_access_name(table_name)}"

    cur.execute(sql)

    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()

    cur.close()

    return pd.DataFrame(rows, columns=cols)


def get_column_info(conn, table_name):
    meta = conn.jconn.getMetaData()
    rs = meta.getColumns(None, None, table_name, "%")

    info = []

    while rs.next():
        col_name = rs.getString("COLUMN_NAME")

        try:
            auto_increment = rs.getString("IS_AUTOINCREMENT")
        except Exception:
            auto_increment = "UNKNOWN"

        info.append({
            "column": col_name,
            "type": rs.getString("TYPE_NAME"),
            "nullable": rs.getString("IS_NULLABLE"),
            "auto_increment": auto_increment
        })

    rs.close()

    return pd.DataFrame(info)


def convert_value(v):
    if pd.isna(v):
        return None

    if isinstance(v, str):
        if v.strip() == "":
            return None
        return v

    if isinstance(v, np.integer):
        return int(v)

    if isinstance(v, np.floating):
        return float(v)

    if isinstance(v, np.bool_):
        return bool(v)

    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()

    return v


def clean_csv_dataframe(csv_df):
    """
    Cleans CSV data before inserting into MDB.
    """

    csv_df = csv_df.copy()

    # Remove completely empty rows
    csv_df = csv_df.dropna(how="all")

    # Strip column names
    csv_df.columns = [str(c).strip() for c in csv_df.columns]

    # Replace empty strings with NaN
    csv_df = csv_df.replace(r"^\s*$", np.nan, regex=True)

    return csv_df


def validate_csv_columns(csv_df, table_columns):
    csv_cols = list(csv_df.columns)

    missing_in_table = [c for c in csv_cols if c not in table_columns]
    missing_in_csv = [c for c in table_columns if c not in csv_cols]

    return missing_in_table, missing_in_csv


def delete_all_rows(conn, table_name):
    cur = conn.cursor()
    sql = f"DELETE FROM {quote_access_name(table_name)}"
    cur.execute(sql)
    conn.commit()
    cur.close()


def insert_dataframe_into_table(
    conn,
    table_name,
    csv_df,
    table_columns,
    auto_increment_columns=None
):
    """
    Inserts CSV dataframe rows into an existing MDB table.
    """

    if auto_increment_columns is None:
        auto_increment_columns = []

    cur = conn.cursor()

    inserted_rows = 0
    skipped_rows = 0

    for _, row in csv_df.iterrows():

        insert_cols = []
        insert_vals = []

        for col in csv_df.columns:

            if col not in table_columns:
                continue

            val = row[col]

            # Skip blank AutoNumber fields
            if col in auto_increment_columns and pd.isna(val):
                continue

            insert_cols.append(col)
            insert_vals.append(convert_value(val))

        if len(insert_cols) == 0:
            skipped_rows += 1
            continue

        col_clause = ", ".join([quote_access_name(c) for c in insert_cols])
        placeholders = ", ".join(["?"] * len(insert_cols))

        sql = (
            f"INSERT INTO {quote_access_name(table_name)} "
            f"({col_clause}) VALUES ({placeholders})"
        )

        cur.execute(sql, insert_vals)
        inserted_rows += 1

    conn.commit()
    cur.close()

    return inserted_rows, skipped_rows


def replace_table_with_csv(
    conn,
    table_name,
    csv_df,
    skip_autonumber_blank=True
):
    """
    Fully replaces selected MDB table values with CSV data.

    Step 1: DELETE all rows from selected table
    Step 2: INSERT CSV rows into selected table
    """

    table_info = get_column_info(conn, table_name)
    table_columns = table_info["column"].tolist()

    auto_increment_columns = []

    if skip_autonumber_blank:
        if "auto_increment" in table_info.columns:
            auto_increment_columns = table_info[
                table_info["auto_increment"].astype(str).str.upper() == "YES"
            ]["column"].tolist()

    # Validate CSV columns
    csv_cols = list(csv_df.columns)

    invalid_cols = [c for c in csv_cols if c not in table_columns]

    if len(invalid_cols) > 0:
        raise ValueError(
            "CSV has columns that do not exist in the MDB table: "
            + ", ".join(invalid_cols)
        )

    delete_all_rows(conn, table_name)

    inserted_rows, skipped_rows = insert_dataframe_into_table(
        conn=conn,
        table_name=table_name,
        csv_df=csv_df,
        table_columns=table_columns,
        auto_increment_columns=auto_increment_columns
    )

    return inserted_rows, skipped_rows, auto_increment_columns


def create_backup_copy(uploaded_file, workdir):
    original_path = os.path.join(workdir, safe_filename(uploaded_file.name))
    edited_path = os.path.join(workdir, "updated_" + safe_filename(uploaded_file.name))

    with open(original_path, "wb") as f:
        f.write(uploaded_file.getbuffer())

    shutil.copy(original_path, edited_path)

    return original_path, edited_path


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.header("⚙️ Settings")

    uploaded_mdb = st.file_uploader(
        "Upload MDB / ACCDB file",
        type=["mdb", "accdb"]
    )

    st.divider()

    preview_rows = st.number_input(
        "Rows to preview from selected table",
        min_value=10,
        max_value=100000,
        value=1000,
        step=100
    )

    st.divider()

    skip_autonumber_blank = st.checkbox(
        "Skip blank AutoNumber columns during insert",
        value=True,
        help="Recommended. If OBJECTID/ID is AutoNumber and blank in CSV, Access will generate it automatically."
    )

    st.warning(
        "Replace mode deletes all existing records in the selected table and inserts CSV records."
    )


# ============================================================
# UPLOAD MDB
# ============================================================

if uploaded_mdb is None:
    st.info("Upload your MDB or ACCDB file to start.")
    st.stop()


if "workdir" not in st.session_state:
    st.session_state.workdir = tempfile.mkdtemp()

workdir = st.session_state.workdir

uploaded_signature = f"{uploaded_mdb.name}_{uploaded_mdb.size}"

if st.session_state.get("uploaded_signature") != uploaded_signature:
    st.session_state["uploaded_signature"] = uploaded_signature

    original_path, edited_path = create_backup_copy(uploaded_mdb, workdir)

    st.session_state["original_path"] = original_path
    st.session_state["edited_path"] = edited_path

    if "replace_done" in st.session_state:
        del st.session_state["replace_done"]

edited_path = st.session_state["edited_path"]


# ============================================================
# CONNECT MDB
# ============================================================

try:
    conn = connect_access_db(edited_path)
except Exception as e:
    st.error(f"Could not open MDB file using UCanAccess: {e}")
    st.stop()


try:
    tables = list_tables(conn)
except Exception as e:
    conn.close()
    st.error(f"Could not list tables: {e}")
    st.stop()


st.success(f"Database opened successfully. Tables found: {len(tables)}")


# ============================================================
# SELECT TABLE
# ============================================================

st.subheader("1️⃣ Select table to view / replace")

table_name = st.selectbox(
    "Select table",
    options=tables
)

table_info = get_column_info(conn, table_name)

with st.expander("Table column information"):
    st.dataframe(table_info, use_container_width=True)

try:
    current_df = read_table(conn, table_name, limit=preview_rows)
except Exception as e:
    st.error(f"Could not read selected table: {e}")
    conn.close()
    st.stop()

st.write(f"Current table preview: **{table_name}**")
st.dataframe(current_df, use_container_width=True)


# ============================================================
# UPLOAD CSV
# ============================================================

st.subheader("2️⃣ Upload CSV to replace selected table")

uploaded_csv = st.file_uploader(
    "Upload CSV file with columns matching the selected MDB table",
    type=["csv"]
)

if uploaded_csv is None:
    st.info("Upload a CSV file to continue.")
    conn.close()
    st.stop()


try:
    csv_df = pd.read_csv(uploaded_csv)
    csv_df = clean_csv_dataframe(csv_df)
except Exception as e:
    st.error(f"Could not read CSV file: {e}")
    conn.close()
    st.stop()


st.write("Uploaded CSV preview")
st.dataframe(csv_df.head(100), use_container_width=True)

table_columns = table_info["column"].tolist()

missing_in_table, missing_in_csv = validate_csv_columns(csv_df, table_columns)

col1, col2 = st.columns(2)

with col1:
    st.write("CSV rows:", len(csv_df))
    st.write("CSV columns:", len(csv_df.columns))

with col2:
    st.write("MDB table columns:", len(table_columns))


if len(missing_in_table) > 0:
    st.error(
        "These CSV columns do not exist in the selected MDB table:\n\n"
        + ", ".join(missing_in_table)
    )
    conn.close()
    st.stop()

if len(missing_in_csv) > 0:
    st.warning(
        "These MDB table columns are not present in CSV. "
        "They will be left as NULL/default only if the database allows it:\n\n"
        + ", ".join(missing_in_csv)
    )


# ============================================================
# REPLACE TABLE
# ============================================================

st.subheader("3️⃣ Replace selected MDB table with CSV")

st.error(
    f"You are about to DELETE all rows from table `{table_name}` "
    f"and INSERT {len(csv_df)} rows from the uploaded CSV."
)

confirm = st.checkbox(
    f"I understand and want to replace table `{table_name}` with the uploaded CSV."
)

replace_button = st.button(
    "🔁 Replace table with CSV",
    type="primary",
    disabled=not confirm
)


if replace_button:
    try:
        inserted_rows, skipped_rows, auto_increment_columns = replace_table_with_csv(
            conn=conn,
            table_name=table_name,
            csv_df=csv_df,
            skip_autonumber_blank=skip_autonumber_blank
        )

        st.session_state["replace_done"] = True

        st.success(
            f"Table `{table_name}` replaced successfully. "
            f"Inserted rows: {inserted_rows}; Skipped rows: {skipped_rows}."
        )

        if len(auto_increment_columns) > 0:
            st.info(
                "Skipped AutoNumber columns during insert: "
                + ", ".join(auto_increment_columns)
            )

    except Exception as e:
        st.error(f"Could not replace table: {e}")


# ============================================================
# DOWNLOAD UPDATED MDB
# ============================================================

st.subheader("4️⃣ Download updated MDB")

try:
    conn.close()
except Exception:
    pass

with open(edited_path, "rb") as f:
    st.download_button(
        label="⬇️ Download updated MDB",
        data=f,
        file_name=os.path.basename(edited_path),
        mime="application/octet-stream",
        type="primary"
    )

st.info(
    "After downloading, test the updated MDB in ArcSWAT. "
    "Keep your original MDB safely as backup."
)
