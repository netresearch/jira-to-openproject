"""Placeholder for former bulk-user-creation tests.

The tests that used to live here (``test_create_users_in_bulk``,
``test_user_migration_create_missing_users``,
``test_user_migration_create_missing_users_no_unmatched``,
``test_user_migration_create_missing_users_with_existing_email``,
``test_bulk_creation_error_handling``) targeted
``OpenProjectClient.create_users_in_bulk``, which no longer exists. Bulk
user creation now goes through the generic ``bulk_create_records`` helper
driven by ``UserMigration.create_missing_users``; that flow is covered in
``tests/functional/test_user_migration.py::TestUserMigration::test_create_missing_users``
and the OpenProject client's own unit tests. The legacy tests were
deleted rather than rewritten because they asserted against a specific
Rails script output format that is no longer produced.
"""
