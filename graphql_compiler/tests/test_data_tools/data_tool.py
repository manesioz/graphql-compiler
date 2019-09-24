# Copyright 2018-present Kensho Technologies, LLC.
import datetime
from decimal import Decimal
from glob import glob
from os import path

from funcy import retry
from redisgraph import Edge, Node
import six
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateSchema

from ...global_utils import merge_non_overlapping_dicts
from ..integration_tests.integration_backend_config import (
    EXPLICIT_DB_BACKENDS, SQL_BACKEND_TO_CONNECTION_STRING, SqlTestBackend
)
from ..test_helpers import get_sqlalchemy_schema_info


def get_integration_data():
    # Map vertex classes tuples containing the properties of each vertex of that class
    vertex_values = {
        'Animal': (
            {
                'uuid': 'cfc6e625-8594-0927-468f-f53d864a7a51',
                'name': 'Animal 1',
                'net_worth': 100,
                'birthday': datetime.date(1900, 1, 1),
            }, {
                'uuid': 'cfc6e625-8594-0927-468f-f53d864a7a52',
                'name': 'Animal 2',
                'net_worth': 200,
                'birthday': datetime.date(1950, 2, 2),
            }, {
                'uuid': 'cfc6e625-8594-0927-468f-f53d864a7a53',
                'name': 'Animal 3',
                'net_worth': 300,
                'birthday': datetime.date(1975, 3, 3),
            }, {
                'uuid': 'cfc6e625-8594-0927-468f-f53d864a7a54',
                'name': 'Animal 4',
                'net_worth': 400,
                'birthday': datetime.date(2000, 4, 4),
            },
        ),
        'Species': (
            {
                'uuid': 'c2c14d8b-0e13-4e64-be63-c86704161850',
                'name': 'Species 1',
            }, {
                'uuid': '35d33f6a-14ab-4b5c-a797-ee9ab817c1fb',
                'name': 'Species 2',
            },
        ),
    }

    # Map edges to tuples containing the source and destination vertex of each edge of that class
    edge_values = {
        'Entity_Related': (
            {
                'from_uuid': 'c2c14d8b-0e13-4e64-be63-c86704161850',
                'to_uuid': '35d33f6a-14ab-4b5c-a797-ee9ab817c1fb',
            },
        )
    }

    # Find the class name of each vertex uuid
    uuid_to_class_name = {}
    for vertex_name, values in six.iteritems(vertex_values):
        for value in values:
            if value['uuid'] in uuid_to_class_name:
                raise AssertionError(u'Duplicate uuid found {}'.format(value['uuid']))
            uuid_to_class_name[value['uuid']] = vertex_name

    return vertex_values, edge_values, uuid_to_class_name


def generate_orient_snapshot_data(client):
    """Create OrientDB test DB from the SQL commands file for snapshot testing."""
    project_root = path.dirname(path.abspath(__file__))
    sql_files = glob(path.join(project_root, 'snapshot_data/*.sql'))

    _load_sql_files_to_orient_client(client, sql_files)


def generate_orient_integration_data(client):
    """Create OrientDB test DB from the SQL commands file for snapshot testing."""
    vertex_values, edge_values, uuid_to_class_name = get_integration_data()
    for vertex_name, vertices in six.iteritems(vertex_values):
        for vertex_props in vertices:
            # XXX SQL injection
            # XXX string formatting is bad for some types
            command = 'CREATE VERTEX {} SET '.format(vertex_name) + ', '.join(
                '{} = \'{}\''.format(key, str(value))
                for key, value in six.iteritems(vertex_props)
            )
            client.command(command)
    for edge_name, edges in six.iteritems(edge_values):
        for edge_spec in edges:
            # XXX SQL injection
            from_classname = uuid_to_class_name[edge_spec['from_uuid']]
            to_classname = uuid_to_class_name[edge_spec['to_uuid']]
            vertex_template = 'SELECT FROM {} WHERE uuid = \'{}\''
            from_selection = vertex_template.format(from_classname, edge_spec['from_uuid'])
            to_selection = vertex_template.format(to_classname, edge_spec['to_uuid'])
            command = 'CREATE EDGE FROM ({}) TO ({})'.format(from_selection, to_selection)
            client.command(command)


def generate_neo4j_integration_data(client):
    """Create Neo4j test DB from the SQL commands file for integration testing."""
    vertex_values, edge_values, _ = get_integration_data()
    with client.driver.session() as session:
        session.run('match (n) detach delete n')  # XXX does this belong here?
        for vertex_name, vertices in six.iteritems(vertex_values):
            for vertex_props in vertices:
                # XXX SQL injection. Validate keys are made of letters
                command = 'create (:{} {{{}}})'.format(vertex_name, ', '.join(
                    '{}: ${}'.format(key, key) for key in vertex_props))
                session.run(command, vertex_props)
        for edge_name, edges in six.iteritems(edge_values):
            for edge_spec in edges:
                # XXX SQL injection.
                command = '''
                    match (a {uuid: $from_uuid}) (b {uuid: $to_uuid})
                    create (a)-[:$edge_name]->(b)'
                '''
                args = {
                    'from_uuid': edge_spec['from_uuid'],
                    'to_uuid': edge_spec['to_uuid'],
                    'edge_name': edge_name,
                }
                session.run(command, args)


def generate_redisgraph_integration_data(client):
    """Create Redisgraph test DB from the SQL commands file for integration testing."""
    vertex_values, edge_values, _ = get_integration_data()
    client.query('create (n)')  # XXX does this belong here?
    client.query('match (n) delete n')  # XXX does this belong here?
    uuid_to_node = {}
    for vertex_name, vertices in six.iteritems(vertex_values):
        for vertex_props in vertices:
            uuid_to_node[vertex_props['uuid']] = Node(label=vertex_name, properties={
                key: value
                for key, value in six.iteritems(vertex_props)
                if not isinstance(value, (datetime.date, datetime.datetime))
            })
            client.add_node(uuid_to_node[vertex_props['uuid']])
    for edge_name, edges in six.iteritems(edge_values):
        for edge_spec in edges:
            client.add_edge(Edge(
                uuid_to_node[edge_spec['from_uuid']],
                edge_name,
                uuid_to_node[edge_spec['to_uuid']],
            ))
    client.commit()


def _load_sql_files_to_orient_client(client, sql_files):
    """Load list of supplied SQL files into the supplied OrientDB client."""
    for filepath in sql_files:
        with open(filepath) as f:
            for command in f.readlines():
                sanitized_command = command.strip()
                if len(sanitized_command) == 0 or sanitized_command[0] == '#':
                    # comment or empty line, ignore
                    continue

                client.command(sanitized_command)


@retry(tries=1, timeout=1)  # pylint: disable=no-value-for-parameter
def init_sql_integration_test_backends():
    """Connect to and open transaction on each SQL DB under test."""
    sql_test_backends = {}
    for backend_name, base_connection_string in six.iteritems(SQL_BACKEND_TO_CONNECTION_STRING):
        engine = create_engine(base_connection_string)
        # safely create the test DATABASE for all SQL backends except sqlite
        # sqlite's in-memory database does not need to be explicitly created/dropped.
        if backend_name in EXPLICIT_DB_BACKENDS:
            # Drop databases if they exist
            engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                text('DROP DATABASE IF EXISTS db_1;'))
            engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                text('DROP DATABASE IF EXISTS db_2;'))

            # create the test databases
            engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                text('CREATE DATABASE db_1;'))
            engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                text('CREATE DATABASE db_2;'))

            # create the test schemas in db_1
            db_1_engine = create_engine(base_connection_string + u'/db_1')
            db_1_engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                CreateSchema('schema_1'))
            db_1_engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                CreateSchema('schema_2'))

            # create the test schemas in db_2
            db_2_engine = create_engine(base_connection_string + u'/db_2')
            db_2_engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                CreateSchema('schema_1'))
            db_2_engine.execution_options(isolation_level='AUTOCOMMIT').execute(
                CreateSchema('schema_2'))

        sql_test_backend = SqlTestBackend(engine, base_connection_string)
        sql_test_backends[backend_name] = sql_test_backend
    return sql_test_backends


def tear_down_integration_test_backends(sql_test_backends):
    """Rollback backends' transactions to wipe test data and to close the active connections."""
    for backend_name, sql_test_backend in six.iteritems(sql_test_backends):
        if backend_name not in EXPLICIT_DB_BACKENDS:
            continue
        # explicitly release engine resources, specifically to disconnect from active DB
        # some backends including Postgres do no not allow an in use DB to be dropped
        sql_test_backend.engine.dispose()
        # connect to base server, not explicit DB, so DB can be dropped
        engine = create_engine(sql_test_backend.base_connection_string)
        # set execution options to AUTOCOMMIT so that the DB drop is not performed in a transaction
        # as this is not allowed on some SQL backends
        engine.execution_options(isolation_level='AUTOCOMMIT').execute(
            text('DROP DATABASE IF EXISTS db_1;'))
        engine.execution_options(isolation_level='AUTOCOMMIT').execute(
            text('DROP DATABASE IF EXISTS db_2;'))


def generate_sql_integration_data(sql_test_backends):
    """Populate test data for SQL backends for integration testing."""
    sql_schema_info = get_sqlalchemy_schema_info()
    vertex_values, edge_values, uuid_to_class_name = get_integration_data()

    # Represent all edges as foreign keys
    uuid_to_foreign_key_values = {}
    for edge_name, edge_values in six.iteritems(edge_values):
        for edge_value in edge_values:
            from_classname = uuid_to_class_name[edge_value['from_uuid']]
            edge_field_name = 'out_{}'.format(edge_name)
            join_descriptor = sql_schema_info.join_descriptors[from_classname][edge_field_name]
            if join_descriptor.to_column != 'uuid':
                raise NotImplementedError(u'Expected to_column to be uuid, found {} for {}'
                                          .format(join_descriptor.to_column, edge_name))

            existing_foreign_key_values = uuid_to_foreign_key_values.setdefault(
                edge_value['from_uuid'], {})
            if join_descriptor.from_column in existing_foreign_key_values:
                raise NotImplementedError(u'The SQL backend does not support many-to-many edges.'
                                          u'Found multiple edges of class {} from vertex {}'
                                          .format(edge_name, edge_value['from_uuid']))
            existing_foreign_key_values[join_descriptor.from_column] = edge_value['to_uuid']

    # Insert all the prepared data into the test database
    for sql_test_backend in six.itervalues(sql_test_backends):
        for vertex_name, insert_values in six.iteritems(vertex_values):
            table = sql_schema_info.vertex_name_to_table[vertex_name]
            table.delete(bind=sql_test_backend.engine)
            table.create(bind=sql_test_backend.engine)
            for insert_value in insert_values:
                foreign_key_values = uuid_to_foreign_key_values.get(insert_value['uuid'], {})
                all_values = merge_non_overlapping_dicts(insert_value, foreign_key_values)
                sql_test_backend.engine.execute(table.insert().values(**all_values))

    return sql_schema_info
