"""`python -m db` (what `make db` runs): brings the schema up to date, then seeds reference data."""
from db import init_db, seed_reference_data

init_db()
seed_reference_data()
