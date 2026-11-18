# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""OpenProject custom field names used by migration and audit tools."""

JIRA_SERVER_SOURCE_LABEL = "Jira Server on-prem 9.11"
JIRA_SOURCE_LABELS = ("jira", JIRA_SERVER_SOURCE_LABEL.lower())

AFFECTS_VERSION = "J2O Affects Version"
ENTITY_TYPE = "J2O Entity Type"
EXTERNAL_ID = "J2O External ID"
EXTERNAL_KEY = "J2O External Key"
EXTERNAL_URL = "J2O External URL"
FIRST_MIGRATION_DATE = "J2O First Migration Date"
JIRA_RESOLUTION = "J2O Jira Resolution"
JIRA_WORKFLOW = "J2O Jira Workflow"
LAST_UPDATE_DATE = "J2O Last Update Date"
ORIGIN_ID = "J2O Origin ID"
ORIGIN_KEY = "J2O Origin Key"
ORIGIN_SYSTEM = "J2O Origin System"
ORIGIN_URL = "J2O Origin URL"
USER_ID = "J2O User ID"
USER_KEY = "J2O User Key"

WORK_PACKAGE_FIELD_SPECS = (
    (ORIGIN_SYSTEM, "string", False),
    (ORIGIN_ID, "string", True),
    (ORIGIN_KEY, "string", True),
    (ORIGIN_URL, "string", False),
    (FIRST_MIGRATION_DATE, "date", False),
    (LAST_UPDATE_DATE, "date", False),
    (JIRA_WORKFLOW, "string", True),
    (JIRA_RESOLUTION, "string", True),
    (AFFECTS_VERSION, "string", True),
)
