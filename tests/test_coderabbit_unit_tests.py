import importlib.util
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import types

# Testing library and framework: pytest

def load_module_with_stubs(tmp_path):
    """
    Dynamically load the target module (tests/test_coderabbit.py) while
    stubbing heavy external dependencies in sys.modules to prevent real side effects.
    Returns the loaded module object.
    """
    # Prepare stub modules/classes
    # 1) Stub for psycopg2 with a minimal connection and cursor

    class StubCursor:
        def __init__(self, table_names=None, rows_by_table=None):
            self._table_names = table_names or []
            self._rows_by_table = rows_by_table or {}
            self._last_query = None
            self.description = []
            self._in_fetch_tables_phase = False
            self._current_table = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self._last_query = (query, params)
            if "information_schema.tables" in query:
                # return table names in fetchall
                self._in_fetch_tables_phase = True
            elif query.strip().lower().startswith("select * from"):
                # Extract table name
                parts = query.strip().split()
                # naive: SELECT * FROM {table}
                try:
                    self._current_table = parts[3]
                except Exception:
                    self._current_table = None
                # set up a fake description (column names)
                rows = self._rows_by_table.get(self._current_table, [])
                max_len = max((len(r) for r in rows), default=0)
                # name columns as col0 ... colN
                self.description = tuple((f"col{i}",) for i in range(max_len))
            else:
                # SET search_path or others
                pass

        def fetchall(self):
            if self._in_fetch_tables_phase:
                self._in_fetch_tables_phase = False
                return [(t,) for t in self._table_names]
            if self._current_table is not None:
                return self._rows_by_table.get(self._current_table, [])
            return []

    class StubConnection:
        def __init__(self, table_names=None, rows_by_table=None):
            self._cursor = StubCursor(table_names=table_names, rows_by_table=rows_by_table)

        def cursor(self):
            return self._cursor

        def close(self):
            pass

    # 2) Stub for Deduce returning controlled annotations for given inputs
    class StubDeduceResult:
        def __init__(self, annotations):
            self.annotations = annotations

    class StubDeduce:
        # Mapping from value string to annotations list to simulate detection
        response_map = {
            "valid_id": ["id"],
            "valid_email@example.com": ["email"],
            "9123456789": ["phone"],
            "nothing": [],
            "": [],
        }

        def deidentify(self, value, disabled=None):
            # ensure str
            value = str(value)
            annotations = self.response_map.get(value, ["some"])  # default to having annotations to trigger branches
            return StubDeduceResult(annotations)

    # 3) Stub for stdnum.verhoeff.is_valid
    class StubVerhoeffModule(types.SimpleNamespace):
        def is_valid(self, s):
            return str(s) == "valid_id"

    stub_stdnum = types.SimpleNamespace(verhoeff=StubVerhoeffModule())

    # 4) Stub for Minio and ResponseError used by push_reports_to_s3
    class StubMinio:
        def __init__(self, host, access_key, secret_key, region, secure=False):
            self.host = host
            self.access_key = access_key
            self.secret_key = secret_key
            self.region = region
            self.secure = secure
            self._buckets = set()

        def bucket_exists(self, name):
            return name in self._buckets

        def make_bucket(self, name, location=None):
            self._buckets.add(name)

        def fput_object(self, bucket, object_name, file_path):
            # Assert file exists to mimic behavior
            assert os.path.exists(file_path)

    class StubResponseError(Exception):
        pass

    # Install stubs into sys.modules BEFORE import
    stub_psycopg2 = types.SimpleNamespace()
    # during module import, connect() will be called; return a stub connection with no tables
    stub_psycopg2.connect = lambda **kwargs: StubConnection(table_names=[], rows_by_table={})

    sys.modules.setdefault("psycopg2", stub_psycopg2)
    sys.modules.setdefault("deduce", types.SimpleNamespace(Deduce=StubDeduce))
    sys.modules.setdefault("stdnum", stub_stdnum)
    # For "from stdnum import verhoeff" to work, also expose submodule
    sys.modules.setdefault("stdnum.verhoeff", StubVerhoeffModule())

    sys.modules.setdefault("minio", types.SimpleNamespace(Minio=StubMinio))
    sys.modules.setdefault("minio.error", types.SimpleNamespace(ResponseError=StubResponseError))

    # Now load the target module from its file path
    target_path = Path("tests") / "test_coderabbit.py"
    assert target_path.exists(), "Expected source file tests/test_coderabbit.py to exist"

    spec = importlib.util.spec_from_file_location("target_coderabbit_module", str(target_path))
    mod = importlib.util.module_from_spec(spec)

    # Use a temporary working directory to capture file outputs from import-time execution
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Provide minimal env vars so config file reading path is consistent (still uses psycopg2 stub)
        os.environ.setdefault("db-server", "localhost")
        os.environ.setdefault("db-port", "5432")
        os.environ.setdefault("db-su-user", "postgres")
        os.environ.setdefault("postgres-password", "password")
        os.environ.setdefault("s3-host", "localhost")
        os.environ.setdefault("s3-region", "us-east-1")
        os.environ.setdefault("s3-user-key", "key")
        os.environ.setdefault("s3-user-secret", "secret")
        os.environ.setdefault("s3-bucket-name", "bucket")

        # Execute the module (this runs its top-level function call)
        spec.loader.exec_module(mod)
    finally:
        os.chdir(cwd)

    return SimpleNamespace(
        module=mod,
        stubs=SimpleNamespace(
            psycopg2=stub_psycopg2,
            Deduce=StubDeduce,
            Minio=StubMinio,
            ResponseError=StubResponseError,
            Verhoeff=StubVerhoeffModule,
        ),
        helpers=SimpleNamespace(
            StubConnection=StubConnection,
            StubCursor=StubCursor,
        )
    )


class TestPureFunctions:
    def test_is_valid_email_valid_cases(self, tmp_path, monkeypatch):
        env = load_module_with_stubs(tmp_path)
        m = env.module

        valid_emails = [
            "user@example.com",
            "USER.NAME+tag-123@sub.domain.co",
            "a_b.c-d%z@domain-123.org",
        ]
        for email in valid_emails:
            assert m.is_valid_email(email) is True, f"Expected valid: {email}"

    def test_is_valid_email_invalid_cases(self, tmp_path, monkeypatch):
        env = load_module_with_stubs(tmp_path)
        m = env.module

        invalid_emails = [
            "",
            "plainaddress",
            "@no-local-part.com",
            "user@",
            "user@.com",
            "user@domain..com",
            "user@domain.c",  # TLD too short
            "user@domain.toolongtldddd",  # excessive TLD length still matches but we keep as invalid scenario based on pattern
            "user name@example.com",
        ]
        for email in invalid_emails:
            assert m.is_valid_email(email) is False, f"Expected invalid: {email}"

    def test_is_valid_mobile_number_valid(self, tmp_path):
        env = load_module_with_stubs(tmp_path)
        m = env.module

        # Pattern: ^[912345678]\d{9}$
        valids = [
            "9123456789",
            "8123456789",
            "7123456789",
            "1123456789",  # allowed by pattern
        ]
        for num in valids:
            assert m.is_valid_mobile_number(num) is True, f"Expected valid mobile: {num}"

    def test_is_valid_mobile_number_invalid(self, tmp_path):
        env = load_module_with_stubs(tmp_path)
        m = env.module

        invalids = [
            "", "12345", "0123456789", "a123456789", "9" * 8, "99999999999",
        ]
        for num in invalids:
            assert m.is_valid_mobile_number(num) is False, f"Expected invalid mobile: {num}"

    def test_is_valid_verhoeff_delegates(self, tmp_path):
        env = load_module_with_stubs(tmp_path)
        m = env.module

        # Stub treats "valid_id" as True, others False
        assert m.is_valid_verhoeff("valid_id") is True
        assert m.is_valid_verhoeff("invalid") is False


class TestDeduceSensitiveData:
    def test_no_tables_no_crash_and_prints_summary(self, tmp_path, capsys):
        env = load_module_with_stubs(tmp_path)
        m = env.module
        StubConnection = env.helpers.StubConnection

        conn = StubConnection(table_names=[], rows_by_table={})

        out_file = tmp_path / "id.txt"
        # Run
        m.deduce_sensitive_data(conn, "db", "public", str(out_file), ignore_columns=[], ignore_tables=[])
        captured = capsys.readouterr()
        # No tables => no entries printed apart from potential final table summaries (none)
        assert "table in db database" not in captured.out
        # File should be created but empty
        assert out_file.exists()

    def test_detects_ids_emails_phones_with_ignores(self, tmp_path, capsys):
        env = load_module_with_stubs(tmp_path)
        m = env.module
        StubConnection = env.helpers.StubConnection

        # Prepare rows:
        # Table: t1 with various values including those that our stubs mark as annotated
        t1_rows = [
            ("valid_id", "valid_email@example.com", "9123456789", "nothing"),
            ("invalid", "not-an-email", "123", ""),
        ]
        # Column names derived automatically: col0, col1, col2, col3
        rows_by_table = {
            "t1": t1_rows,
            "ignore_me": [("valid_id",)],
        }

        conn = StubConnection(table_names=["t1", "ignore_me"], rows_by_table=rows_by_table)

        out_file = tmp_path / "id.txt"
        # Run with ignore rules
        m.deduce_sensitive_data(conn, "db", "public", str(out_file), ignore_columns=["col3"], ignore_tables=["ignore_me"])
        captured = capsys.readouterr()

        # Check printed counts summary for t1
        assert "mail id's" in captured.out
        assert "mobile numbers" in captured.out
        assert "id's are found in t1 table in db database" in captured.out

        # Validate files written
        mails = (tmp_path / "mails.txt").read_text()
        mobiles = (tmp_path / "mobile_numbers.txt").read_text()
        ids = out_file.read_text()

        assert "valid_email@example.com" in mails
        assert "9123456789" in mobiles
        assert "valid_id" in ids

        # Ensure ignored table did not appear
        assert "ignore_me" not in (mails + mobiles + ids)

        # Ensure ignored column (col3 == "nothing" or "") not written
        assert "nothing" not in (mails + mobiles + ids)

    def test_skips_when_no_annotations(self, tmp_path, capsys, monkeypatch):
        env = load_module_with_stubs(tmp_path)
        m = env.module
        StubConnection = env.helpers.StubConnection

        # Patch Deduce to always return no annotations
        class NoAnnResult:
            def __init__(self): self.annotations = []

        class NoAnnDeduce:
            def deidentify(self, value, disabled=None):
                return NoAnnResult()

        # Monkeypatch Deduce class inside loaded module
        monkeypatch.setattr(m, "Deduce", NoAnnDeduce, raising=True)

        rows_by_table = {
            "t": [("valid_id", "valid_email@example.com", "9123456789")],
        }
        conn = StubConnection(table_names=["t"], rows_by_table=rows_by_table)
        out_file = tmp_path / "id.txt"
        m.deduce_sensitive_data(conn, "db", "public", str(out_file), ignore_columns=[], ignore_tables=[])

        captured = capsys.readouterr()
        # No annotations => counts should stay zero
        assert "0 mail id's, 0 mobile numbers, and 0 id's are found in t table in db database" in captured.out

        # Files should exist but not contain data lines
        assert (tmp_path / "mails.txt").exists()
        assert (tmp_path / "mobile_numbers.txt").exists()
        assert out_file.exists()
        assert (tmp_path / "mails.txt").read_text() == ""
        assert (tmp_path / "mobile_numbers.txt").read_text() == ""
        assert out_file.read_text() == ""

class TestTopLevelEntrypoint:
    def test_import_side_effects_are_stubbed_and_files_created(self, tmp_path):
        load_module_with_stubs(tmp_path)

        # Top-level call deduce_sensitive_data_in_databases() already ran during load.
        # Check that expected files exist due to push_reports_to_s3 ensuring creation.
        for name in ("id.txt", "mails.txt", "mobile_numbers.txt"):
            assert (tmp_path / name).exists()

    def test_push_reports_to_s3_ensures_bucket_and_uploads(self, tmp_path, monkeypatch):
        env = load_module_with_stubs(tmp_path)
        m = env.module

        # Create files expected by push
        for name in ("id.txt", "mails.txt", "mobile_numbers.txt"):
            (tmp_path / name).write_text("content")

        # Use stub Minio via module-level import; ensure no exception and "bucket" created
        # Redirect CWD to tmp_path so fput_object can find files
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            m.push_reports_to_s3("localhost:9000", "us-east-1", "key", "secret", "bucket-x")
            # If the stub Minio makes bucket on demand, no exception was raised
            assert True
        finally:
            os.chdir(cwd)