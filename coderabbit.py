import psycopg2
from configparser import ConfigParser
from stdnum import verhoeff
from deduce import Deduce
#from minio import Minio
#from minio.error import ResponseError
import re
import os

def is_valid_verhoeff(number):
    """
    Return True if the given number passes the Verhoeff checksum algorithm.
    
    Parameters:
        number: A numeric or string value representing the identifier to validate. The value will be converted to a string before validation.
    
    Returns:
        bool: True when the input is a valid Verhoeff number; otherwise False.
    """
    return verhoeff.is_valid(str(number))

def is_valid_email(email):
    """
    Return True if the given value matches a basic email address pattern, False otherwise.
    
    This function converts the input to a string and checks it against a regular expression that requires:
    - a local part containing letters, digits, and the characters . _ % + - 
    - a single '@' separator
    - a domain part containing letters, digits, dots or hyphens and a final dot followed by at least two letters.
    
    Parameters:
        email: Any
            Value to validate; will be coerced to str before matching.
    
    Returns:
        bool: True when the value matches the email pattern, False otherwise.
    """
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    match = email_pattern.match(str(email))
    return bool(match)

def is_valid_mobile_number(phone_number):
    """
    Validate whether a value is a 10-digit Indian-style mobile number matching the pattern: starts with one of 9,1,2,3,4,5,6,7,8 and followed by nine digits.
    
    Parameters:
        phone_number (str|int): The phone number to validate; numeric values will be converted to string.
    
    Returns:
        bool: True if the value matches the 10-digit pattern, False otherwise.
    """
    pattern = re.compile(r'^[912345678]\d{9}$')
    match = re.match(pattern, str(phone_number))
    return bool(match)

def deduce_sensitive_data(connection, database_name, schema_name, output_file, ignore_columns, ignore_tables):
    """
    Scan all tables in a schema, run de-identification on each column value, and append detected IDs, emails, and mobile numbers to report files.
    
    For each table in the given schema (skipping any in ignore_tables), every row and non-ignored column is passed to a Deduce instance. When annotations are produced and the column value validates as a Verhoeff ID, email, or mobile number, findings are appended to output files:
    - IDs are written to the provided output_file (appended).
    - Emails are appended to 'mails.txt'.
    - Mobile numbers are appended to 'mobile_numbers.txt'.
    A summary line is printed for each table with counts of emails, mobile numbers, and IDs found.
    
    Parameters:
        connection: Database connection used to query tables and rows (not documented as a service).
        database_name (str): Logical name used in log lines for the current database.
        schema_name (str): PostgreSQL schema to set as the search path and scan for tables.
        output_file (str): Path to the file where ID findings are appended.
        ignore_columns (iterable[str] | None): Column names to skip during scanning.
        ignore_tables (iterable[str] | None): Table names to skip during scanning.
    
    Side effects:
        - Appends to output_file, 'mails.txt', and 'mobile_numbers.txt'.
        - Prints per-table summaries to stdout.
    """
    deduce_instance = Deduce()

    with connection.cursor() as cursor:
        cursor.execute(f"SET search_path TO {schema_name}")
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (schema_name,))
        tables = [table[0] for table in cursor.fetchall()]

        with open(output_file, 'a') as deduced_file:
            for table_name in tables:
                if ignore_tables and table_name in ignore_tables:
                    # print(f"Ignoring Table: {table_name} in Database: {database_name}")
                    continue

                # print(f"Currently checking Table: {table_name} in Database: {database_name}")
                deduced_file.write(f"Currently checking Table: {table_name} in Database: {database_name}\n")

                cursor.execute(f'SELECT * FROM {table_name}')
                rows = cursor.fetchall()

                id_count = 0
                mail_count = 0
                mobile_count = 0

                for row in rows:
                    for i, column_value in enumerate(row):
                        column_name = cursor.description[i][0]

                        if ignore_columns and column_name in ignore_columns:
                            continue

                        deduced_result = deduce_instance.deidentify(
                            str(column_value),
                            disabled={'names', 'institutions', 'locations', 'dates', 'ages', 'urls'}
                        )

                        if deduced_result.annotations and is_valid_verhoeff(column_value):
                            id_count += 1
                            deduced_file.write(f"Column: {column_name}, Data: {column_value}\n")
                            deduced_file.write(f"Deduced Findings: {deduced_result.annotations}\n\n")

                        with open('mobile_numbers.txt', 'a') as file:
                            if deduced_result.annotations and is_valid_mobile_number(column_value):
                                mobile_count += 1
                                file.write(f"Column: {column_name}, Data: {column_value}\n")
                                file.write(f"Deduced Findings: {deduced_result.annotations}\n\n")

                        with open('mails.txt', 'a') as file:
                            if deduced_result.annotations and is_valid_email(column_value):
                                mail_count += 1
                                file.write(f"Column: {column_name}, Data: {column_value}\n")
                                file.write(f"Deduced Findings: {deduced_result.annotations}\n\n")

                print(f"{mail_count} mail id's, {mobile_count} mobile numbers, and {id_count} id's are found in {table_name} table in {database_name} database")

def push_reports_to_s3(s3_host, s3_region, s3_user_key, s3_user_secret, s3_bucket_name):
    """
    Upload local report files (id.txt, mails.txt, mobile_numbers.txt) to a MinIO/S3-compatible bucket.
    
    Ensures the target bucket exists (creates it if missing), ensures the three report files exist locally (creates empty files if needed), and uploads them under the keys:
      - reports/id.txt
      - reports/mails.txt
      - reports/mobile_numbers.txt
    
    Parameters:
        s3_host (str): Hostname (and optional port) of the MinIO/S3 endpoint.
        s3_region (str): Region name to use when creating the bucket.
        s3_user_key (str): Access key / username for the MinIO account.
        s3_user_secret (str): Secret key / password for the MinIO account.
        s3_bucket_name (str): Name of the bucket to upload reports into.
    
    Notes:
        - The function initializes the Minio client with secure=False (HTTP). Set secure=True in the client initialization if HTTPS is required.
        - MinIO-related errors are caught and printed; they are not re-raised.
    """
    mc = Minio(s3_host,
               access_key=s3_user_key,
               secret_key=s3_user_secret,
               region=s3_region,
               secure=False)  # Set secure=True if using HTTPS

    try:
        if not mc.bucket_exists(s3_bucket_name):
            mc.make_bucket(s3_bucket_name, location=s3_region)

        # Ensure files exist before attempting to upload
        for filename in ['id.txt', 'mails.txt', 'mobile_numbers.txt']:
            open(filename, 'a').close()

        mc.fput_object(s3_bucket_name, 'reports/id.txt', 'id.txt')
        mc.fput_object(s3_bucket_name, 'reports/mails.txt', 'mails.txt')
        mc.fput_object(s3_bucket_name, 'reports/mobile_numbers.txt', 'mobile_numbers.txt')

        print("\nReports pushed to MinIO")

    except ResponseError as err:
        print(f"MinIO Error: {err}")

def deduce_sensitive_data_in_databases():
    # Initialize config variable
    """
    Orchestrates reading configuration, scanning databases for sensitive data, writing findings to files, and uploading reports to MinIO.
    
    Reads PostgreSQL and MinIO configuration from environment variables or db.properties, connects to the first listed database, then iterates a configured list of databases and schemas calling deduce_sensitive_data(...) for each. Findings are appended to output files (id.txt, mails.txt, mobile_numbers.txt). After scanning, the function attempts to upload those report files to the configured MinIO/S3-compatible bucket via push_reports_to_s3(...). The database connection is closed when processing completes.
    
    Side effects:
    - Opens a PostgreSQL connection.
    - Writes/updates id.txt, mails.txt, and mobile_numbers.txt on disk.
    - May create or upload objects to the configured MinIO/S3 bucket.
    
    Configuration sources:
    - Primary: environment variables (db-server, db-port, db-su-user, postgres-password, s3-host, s3-region, s3-user-key, s3-user-secret, s3-bucket-name).
    - Fallback: db.properties file with sections "PostgreSQL Connection", "MinIO Connection", "Ignored Tables", and "Ignored Columns".
    
    Note: The function does not return a value; connection errors or S3 errors will propagate from the underlying libraries.
    """
    config = ConfigParser()

    # If environment variables are not set, read from db.properties file
    if not all([os.environ.get('db-server'), os.environ.get('db-port'), os.environ.get('db-su-user'),
                os.environ.get('postgres-password'), os.environ.get('s3-host'), os.environ.get('s3-region'),
                os.environ.get('s3-user-key'), os.environ.get('s3-user-secret'), os.environ.get('s3-bucket-name')]):
        config.read('db.properties')

    # Read PostgreSQL and MinIO details from environment variables or db.properties
    db_server = os.environ.get('db-server') or config.get('PostgreSQL Connection', 'db-server', fallback='')
    db_port = os.environ.get('db-port') or config.get('PostgreSQL Connection', 'db-port', fallback='')
    db_user = os.environ.get('db-su-user') or config.get('PostgreSQL Connection', 'db-su-user', fallback='')
    db_password = os.environ.get('postgres-password') or config.get('PostgreSQL Connection', 'postgres-password', fallback='')

    minio_host = os.environ.get('s3-host') or config.get('MinIO Connection', 's3-host', fallback='')
    minio_region = os.environ.get('s3-region') or config.get('MinIO Connection', 's3-region', fallback='')
    minio_user_key = os.environ.get('s3-user-key') or config.get('MinIO Connection', 's3-user-key', fallback='')
    minio_user_secret = os.environ.get('s3-user-secret') or config.get('MinIO Connection', 's3-user-secret', fallback='')
    minio_bucket_name = os.environ.get('s3-bucket-name') or config.get('MinIO Connection', 's3-bucket-name', fallback='')

    # Read ignored tables and columns from db.properties
    ignore_tables_str = config.get('Ignored Tables', 'ignore_tables', fallback='')
    ignore_columns_str = config.get('Ignored Columns', 'ignore_columns', fallback='')

    ignore_tables = [table.strip() for table in ignore_tables_str.split(',')] if ignore_tables_str else []
    ignore_columns = [column.strip() for column in ignore_columns_str.split(',')] if ignore_columns_str else []

    # Define the databases list
    databases = [
        {"name": "mosip_pms", "schema": "pms"},
        # Add other databases as needed
    ]

    connection = psycopg2.connect(
        host=db_server,
        port=db_port,
        user=db_user,
        password=db_password,
        database=databases[0]['name']
    )

    try:
        output_file_path = 'id.txt'

        for db_info in databases:
            print(f"\nAnalyzing data in Database: {db_info['name']}\n")
            deduce_sensitive_data(connection, db_info['name'], db_info['schema'], output_file_path, ignore_columns,
                                   ignore_tables)

        print(f"\nDeduced findings saved to {output_file_path}, mails.txt, mobile_numbers.txt")

        # Add the following lines to push reports to MinIO
        s3_host = minio_host
        s3_region = minio_region
        s3_user_key = minio_user_key
        s3_user_secret = minio_user_secret
        s3_bucket_name = minio_bucket_name

        push_reports_to_s3(s3_host, s3_region, s3_user_key, s3_user_secret, s3_bucket_name)

    finally:
        connection.close()

# Call the main function
deduce_sensitive_data_in_databases()
