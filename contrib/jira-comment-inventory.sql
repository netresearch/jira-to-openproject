-- Copyright (c) 2026 Netresearch DTT GmbH
-- SPDX-License-Identifier: MIT
-- Run with psql against a frozen PostgreSQL Jira snapshot. No comment text is selected.
WITH by_issue AS (
  SELECT i.id, coalesce(json_agg(a.id::text ORDER BY a.id)
                         FILTER (WHERE a.id IS NOT NULL), '[]'::json) AS comments
  FROM jiraissue AS i
  JOIN project AS p ON p.id = i.project
  LEFT JOIN jiraaction AS a ON a.issueid = i.id AND a.actiontype = 'comment'
  WHERE p.pkey = :'project_key'
  GROUP BY i.id
)
SELECT json_build_object('jira', :'source_url', 'project', :'project_key',
                         'issues', coalesce(json_object_agg(id::text, comments), '{}'::json))
FROM by_issue;
