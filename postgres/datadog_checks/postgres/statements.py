# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
from __future__ import unicode_literals

import copy

import psycopg2
import psycopg2.extras

from datadog_checks.base.log import get_check_logger
from datadog_checks.base.utils.db.sql import compute_sql_signature, normalize_query_tag
from datadog_checks.base.utils.db.statement_metrics import StatementMetrics, apply_row_limits

from .util import milliseconds_to_nanoseconds

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent


STATEMENTS_QUERY = """
SELECT {cols}
  FROM {pg_stat_statements_view} as pg_stat_statements
  LEFT JOIN pg_roles
         ON pg_stat_statements.userid = pg_roles.oid
  LEFT JOIN pg_database
         ON pg_stat_statements.dbid = pg_database.oid
  WHERE pg_database.datname = %s
  AND query != '<insufficient privilege>'
  LIMIT {limit}
"""

DEFAULT_STATEMENTS_LIMIT = 10000

# Required columns for the check to run
PG_STAT_STATEMENTS_REQUIRED_COLUMNS = frozenset({'calls', 'query', 'total_time', 'rows'})

PG_STAT_STATEMENTS_OPTIONAL_COLUMNS = frozenset({'queryid'})

# Monotonically increasing count columns to be converted to metrics
PG_STAT_STATEMENTS_METRIC_COLUMNS = {
    'calls': 'postgresql.queries.count',
    'total_time': 'postgresql.queries.time',
    'rows': 'postgresql.queries.rows',
    'shared_blks_hit': 'postgresql.queries.shared_blks_hit',
    'shared_blks_read': 'postgresql.queries.shared_blks_read',
    'shared_blks_dirtied': 'postgresql.queries.shared_blks_dirtied',
    'shared_blks_written': 'postgresql.queries.shared_blks_written',
    'local_blks_hit': 'postgresql.queries.local_blks_hit',
    'local_blks_read': 'postgresql.queries.local_blks_read',
    'local_blks_dirtied': 'postgresql.queries.local_blks_dirtied',
    'local_blks_written': 'postgresql.queries.local_blks_written',
    'temp_blks_read': 'postgresql.queries.temp_blks_read',
    'temp_blks_written': 'postgresql.queries.temp_blks_written',
}

# Columns to apply as tags
PG_STAT_STATEMENTS_TAG_COLUMNS = {
    'datname': 'db',
    'rolname': 'user',
    'query': 'query',
}

# These limits define the top K and bottom K unique query rows for each metric. For each check run the
# max metrics sent will be sum of all numbers below (in practice, much less due to overlap in rows).
DEFAULT_STATEMENT_METRICS_LIMITS = {
    'calls': (400, 0),
    'total_time': (400, 0),
    'rows': (400, 0),
    'shared_blks_hit': (50, 0),
    'shared_blks_read': (50, 0),
    'shared_blks_dirtied': (50, 0),
    'shared_blks_written': (50, 0),
    'local_blks_hit': (50, 0),
    'local_blks_read': (50, 0),
    'local_blks_dirtied': (50, 0),
    'local_blks_written': (50, 0),
    'temp_blks_read': (50, 0),
    'temp_blks_written': (50, 0),
    # Synthetic column limits
    'avg_time': (400, 0),
    'shared_blks_ratio': (0, 50),
}


def generate_synthetic_rows(rows):
    """
    Given a list of rows, generate a new list of rows with "synthetic" column values derived from
    the existing row values.
    """
    synthetic_rows = []
    for row in rows:
        new = copy.copy(row)
        new['avg_time'] = float(new['total_time']) / new['calls'] if new['calls'] > 0 else 0
        new['shared_blks_ratio'] = (
            float(new['shared_blks_hit']) / (new['shared_blks_hit'] + new['shared_blks_read'])
            if new['shared_blks_hit'] + new['shared_blks_read'] > 0
            else 0
        )

        synthetic_rows.append(new)

    return synthetic_rows


class PostgresStatementMetrics(object):
    """Collects telemetry for SQL statements"""

    def __init__(self, config):
        self.config = config
        self.log = get_check_logger()
        self._state = StatementMetrics()

    def _execute_query(self, cursor, query, params=()):
        try:
            cursor.execute(query, params)
            return cursor.fetchall()
        except (psycopg2.ProgrammingError, psycopg2.errors.QueryCanceled) as e:
            self.log.warning('Statement-level metrics are unavailable: %s', e)
            return []

    def _get_pg_stat_statements_columns(self, db):
        """
        Load the list of the columns available under the `pg_stat_statements` table. This must be queried because
        version is not a reliable way to determine the available columns on `pg_stat_statements`. The database can
        be upgraded without upgrading extensions, even when the extension is included by default.
        """
        # Querying over '*' with limit 0 allows fetching only the column names from the cursor without data
        query = STATEMENTS_QUERY.format(
            cols='*',
            pg_stat_statements_view=self.config.pg_stat_statements_view,
            limit=0,
        )
        cursor = db.cursor()
        self._execute_query(cursor, query, params=(self.config.dbname,))
        colnames = [desc[0] for desc in cursor.description]
        return colnames

    def collect_per_statement_metrics(self, db):
        try:
            return self._collect_per_statement_metrics(db)
        except Exception:
            db.rollback()
            self.log.exception('Unable to collect statement metrics due to an error')
            return []

    def _collect_per_statement_metrics(self, db):
        metrics = []

        available_columns = self._get_pg_stat_statements_columns(db)
        missing_columns = PG_STAT_STATEMENTS_REQUIRED_COLUMNS - set(available_columns)
        if len(missing_columns) > 0:
            self.log.warning(
                'Unable to collect statement metrics because required fields are unavailable: %s',
                ', '.join(list(missing_columns)),
            )
            return metrics

        desired_columns = (
            list(PG_STAT_STATEMENTS_METRIC_COLUMNS.keys())
            + list(PG_STAT_STATEMENTS_OPTIONAL_COLUMNS)
            + list(PG_STAT_STATEMENTS_TAG_COLUMNS.keys())
        )
        query_columns = list(set(desired_columns) & set(available_columns) | set(PG_STAT_STATEMENTS_TAG_COLUMNS.keys()))
        rows = self._execute_query(
            db.cursor(cursor_factory=psycopg2.extras.DictCursor),
            STATEMENTS_QUERY.format(
                cols=', '.join(query_columns),
                pg_stat_statements_view=self.config.pg_stat_statements_view,
                limit=DEFAULT_STATEMENTS_LIMIT,
            ),
            params=(self.config.dbname,),
        )
        if not rows:
            return metrics

        def row_keyfunc(row):
            # old versions of pg_stat_statements don't have a query ID so fall back to the query string itself
            queryid = row['queryid'] if 'queryid' in row else row['query']
            return (queryid, row['datname'], row['rolname'])

        rows = self._state.compute_derivative_rows(rows, PG_STAT_STATEMENTS_METRIC_COLUMNS.keys(), key=row_keyfunc)
        metrics.append(('dd.postgres.queries.query_rows_raw', len(rows), []))

        rows = generate_synthetic_rows(rows)
        rows = apply_row_limits(
            rows,
            self.config.statement_metrics_limits or DEFAULT_STATEMENT_METRICS_LIMITS,
            tiebreaker_metric='calls',
            tiebreaker_reverse=True,
            key=row_keyfunc,
        )
        metrics.append(('dd.postgres.queries.query_rows_limited', len(rows), []))

        for row in rows:
            try:
                normalized_query = datadog_agent.obfuscate_sql(row['query'])
                if not normalized_query:
                    self.log.warning("Obfuscation of query '%s' resulted in empty query", row['query'])
                    continue
            except Exception as e:
                # If query obfuscation fails, it is acceptable to log the raw query here because the
                # pg_stat_statements table contains no parameters in the raw queries.
                self.log.warning("Failed to obfuscate query '%s': %s", row['query'], e)
                continue

            query_signature = compute_sql_signature(normalized_query)

            # All "Deep Database Monitoring" statement-level metrics are tagged with a `query_signature`
            # which uniquely identifies the normalized query family. Where possible, this hash should
            # match the hash of APM "resources" (https://docs.datadoghq.com/tracing/visualization/resource/)
            # when the resource is a SQL query. Postgres' query normalization in the `pg_stat_statements` table
            # preserves most of the original query, so we tag the `resource_hash` with the same value as the
            # `query_signature`. The `resource_hash` tag should match the *actual* APM resource hash most of
            # the time, but not always. So this is a best-effort approach to link these metrics to APM metrics.
            tags = ['query_signature:' + query_signature, 'resource_hash:' + query_signature]

            for column, tag_name in PG_STAT_STATEMENTS_TAG_COLUMNS.items():
                if column not in row:
                    continue
                value = row[column]
                if column == 'query':
                    value = normalize_query_tag(normalized_query)
                tags.append('{tag_name}:{value}'.format(tag_name=tag_name, value=value))

            for column, metric_name in PG_STAT_STATEMENTS_METRIC_COLUMNS.items():
                if column not in row:
                    continue
                value = row[column]
                if column == 'total_time':
                    # All "Deep Database Monitoring" timing metrics are in nanoseconds
                    # Postgres tracks pg_stat* timing stats in milliseconds
                    value = milliseconds_to_nanoseconds(value)
                metrics.append((metric_name, value, tags))

        return metrics
