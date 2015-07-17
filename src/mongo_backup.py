#!/usr/bin/env python3

import sys
import os
sys.path.append(
    os.path.dirname(__file__)
)

import logging
import os
import time
import pymongo
import yaml
import argparse

from datetime import datetime as dt
from datetime import timedelta
from pymongo import MongoClient
from bson.objectid import ObjectId

from backup_logger import LOGGER


DEFAULT_CONFIG_FILE    = os.path.join(
    os.path.dirname(__file__),
    'config.yaml'
)
DEFAULT_PROGRESS_FILE  = os.path.join(
    os.path.dirname(__file__),
    'current_progress.yaml'
)
DEFAULT_LOG_FILE       = os.path.join(
    os.path.dirname(__file__),
    'mongo_backup.log'
)

DEFAULT_CONFIG = {'collections': {},
                  'source_db': 'mongodb://localhost/test_db',
                  'destination_db': 'mongodb://localhost/dest_db',
                  'rate': 60000,
                  'stop': False}

CONNECTIONS = []
LAST_TIME   = dt.now()


def create_file_if_not_exists(path, content=''):
    """
    Creates a file with a pre-defined content if not exists yet.
    """
    try:
        with open(path, 'r') as f:
            pass
    except Exception:
        with open(path, 'w') as f:
            f.write(content)


def print_collection_size(coll, logger=None):
    """
    Prints collection size.
    """
    logger = logger or LOGGER
    logger.info("{}: {} document(s)".format(coll.name, coll.count()))


def read_config(path=None):
    """
    Reads YAML config file and converts it to Python dictionary.  By default
    the file is located at DEFAULT_CONFIG_FILE.  If the config file doesn't
    exist, it is created with content DEFAULT_CONFIG.
    """
    path = path or DEFAULT_CONFIG_FILE
    create_file_if_not_exists(
        path=path,
        content=yaml.dump(DEFAULT_CONFIG)
    )

    res = {}

    try:
        with open(path, 'r') as input:
            res = yaml.load(input)
    except Exception as e:
        sys.stderr.write(
            "Invalid YAML syntax in config.yaml: {}\n",
            str(e)
        )
        sys.exit(1)

    check_stop_flag(res)

    return res


def close_connections():
    """
    Gracefully closes all connections.
    """
    for conn in CONNECTIONS:
        conn.close()


def check_stop_flag(config, logger=LOGGER):
    """
    Checks if the stop flag presents in config and stop the application
    gracefully if it is.
    """
    if not config.get('stop', False):
        return

    logger = logger or LOGGER
    logger.info('gracefully stopped by user')

    close_connections()

    sys.exit(0)


def report_collections_size(db, coll_names, logger=None):
    """
    Reports size of all collections.
    """
    logger = logger or LOGGER

    logger.info("all collection size:")

    for name in coll_names:
        print_collection_size(
            db[name],
            logger=logger
        )


def milisecs_passed(last_time=None):
    """
    Calculates time passed since last_time in miliseconds.
    """
    last_time = last_time or LAST_TIME
    delta = int((dt.now() - LAST_TIME).total_seconds() * 1000)
    return delta


def update_last_time(new_value=None):
    """
    Updates `LAST_TIME'.
    """
    global LAST_TIME
    LAST_TIME = new_value or dt.now()


def balance_rate(unit=None, last_time=None):
    """
    Sleeps if necessary to keep up with current backup rates and updates
    current execution time to LAST_TIME.
    """
    last_time = last_time or LAST_TIME
    unit      = unit or 1000

    delta = milisecs_passed()
    if delta < unit:
        time.sleep((unit - delta) / 1000)
        update_last_time()


# LAST_TIME = dt.now(); balance_rate(); milisecs_passed()


def find_docs_to_update(coll,
                        condition=None,
                        progress_path=None,
                        logger=None):
    """
    Builds and queries list of docs to update in `coll'.  If `condition' is
    None or not supplied, find all documents.  TODO: documentation about
    grammar that `condition' supports.
    """
    if not condition or condition == [] or condition == {}:
        return coll.find()

    logger        = logger or LOGGER
    progress_path = progress_path or DEFAULT_PROGRESS_FILE
    method        = condition['method']
    name          = coll.name

    create_file_if_not_exists(progress_path, yaml.dump({}))

    if method == 'object_id':
        # Find all documents having IDs greater than the saved Object ID
        with open(progress_path, 'r') as input:
            start_id = yaml.load(input).get(name, '')

        if start_id == '':
            return coll.find()
        else:
            logger.info('starting from ObjectId: %s', start_id)
            return coll.find({ "_id": { "$gt": ObjectId(start_id) }})

    elif method == 'date_delta':
        # Find all documents having 'date' field ≥ now() - delta
        delta = timedelta(**{ condition['unit']: condition['value']})
        start_date = (dt.now().date() - delta).strftime('%Y-%m-%d')

        logger.info('starting from date: %s', start_date)
        return coll.find({ 'date': { "$gte": start_date } })
        

# adb = MongoClient('mongodb://localhost/)
# find_docs_to_update(adb.log_traffic).count()
# log_last_doc('log_traffic', '555317f7d290053143db66b2')
# find_docs_to_update(adb.log_traffic, { 'method': 'object_id' }).count()
# => 58
# log_last_doc('log_traffic', '555317f7d290053143db668a')
# find_docs_to_update(adb.log_traffic, { 'method': 'object_id' }).count()
# => 98


def log_last_doc(coll_name, doc_id, logger=None, path=None):
    """
    Logs last document inserted into `path' as YAML.
    """
    logger  = logger or LOGGER
    path    = path or DEFAULT_PROGRESS_FILE

    create_file_if_not_exists(path=path, content=yaml.dump({}))

    with open(path, 'r') as input:
        progress = yaml.load(input)

    progress[coll_name] = doc_id

    with open(path, 'w') as output:
        output.write(yaml.dump(progress))

    logger.info('last document ID: %s in %s', doc_id, coll_name)


def backup_collection(coll_src,
                      coll_dest,
                      condition=None,
                      config_path=None,
                      logger=None):
    """
    Backups collection from coll_src to coll_dest with a pre-defined search
    condition.
    """
    logger        = logger or LOGGER
    current_docs  = []
    config        = read_config(path=config_path)
    docs          = find_docs_to_update(coll_src, condition)

    logger.info(
        "backing up %s (%s docs) ⇒ %s (%s docs)",
        coll_src.name,
        coll_src.count(),
        coll_dest.name,
        coll_dest.count()
    )
    logger.info('rate: %s doc(s)/sec', config['rate'])

    update_last_time()

    def insert_to_dest():
        nonlocal config
        nonlocal current_docs

        logger.info(
            'bulk inserting: %s → %s',
            len(current_docs),
            coll_dest.name
        )

        try:
            coll_dest.insert_many(current_docs, ordered=False)
        except Exception as e:
            pass

        log_last_doc(
            coll_name=coll_dest.name,
            doc_id=str(current_docs[-1]['_id'])
        )

        balance_rate()
        config       = read_config()
        current_docs = []

    for doc in docs:
        current_docs.append(doc)
        if len(current_docs) >= config['rate']:
            insert_to_dest()

    if len(current_docs) != 0:
        insert_to_dest()


# adb = MongoClient('mongodb://localhost/)
# backup_collection(adb.log_traffic, adb.log_traffic_2, config_path='/m/src/adflex/db_backup/src/config.yaml')


def get_db(conn_str):
    """
    Retrieves a DB from conn_str.  conn_str is of the following format
    mongodb://[username][[:[password]]@]<host>/<db_name>.
    """
    global CONNECTIONS

    db_name_pos = conn_str.rfind("/") + 1
    db_name     = conn_str[db_name_pos:]
    client      = MongoClient(conn_str)

    try:
        CONNECTIONS.index(client)
        CONNECTIONS.append(client)
    except Exception:
        pass

    return client[db_name]


def read_cmd_args():
    """
    Sets and reads command line arguments.
    """
    parser = argparse.ArgumentParser(
        description='AdFlex MongoDB collection to collection backup tool.'
    )
    parser.add_argument(
        "--config",
        help='specify YAML config file, default: {}'.format(DEFAULT_CONFIG_FILE),
        default=DEFAULT_CONFIG_FILE,
        type=str
    )
    parser.add_argument(
        '--progress-file',
        help='specify YAML progress file, default: {}'.format(DEFAULT_PROGRESS_FILE),
        default=DEFAULT_PROGRESS_FILE,
        type=str
    )
    parser.add_argument(
        '--log',
        help='specify log file, default: {}'.format(DEFAULT_LOG_FILE),
        default=DEFAULT_LOG_FILE,
        type=str
    )
    
    return vars(parser.parse_args())


def set_global_params(args):
    """
    Sets the appropriate global variables based on the passed-in command line
    arguments.
    """
    global DEFAULT_CONFIG_FILE
    global DEFAULT_PROGRESS_FILE
    global DEFAULT_LOG_FILE

    DEFAULT_CONFIG_FILE    = args['config']
    DEFAULT_PROGRESS_FILE  = args['progress_file']
    DEFAULT_LOG_FILE       = args['log']


def main():
    set_global_params(read_cmd_args())

    print(DEFAULT_CONFIG_FILE)
    print(DEFAULT_PROGRESS_FILE)
    print(DEFAULT_LOG_FILE)

    sys.exit(0)

    config   = read_config()
    db_src   = get_db(config['db_source'])
    db_dest  = get_db(config['db_destination'])
    colls    = config['collections']

    for name, condition in colls.items():
        print(name, condition)
        backup_collection(
            coll_src=db_src[name],
            coll_dest=db_dest[name],
            condition=condition
        )

    close_connections()


if __name__ == '__main__':
    main()


#
# use test_db;
# db.log_traffic.count();
# db.log_traffic.find({ _id: { '$gte': ObjectId('555317f7d290053143db668b') } }).count();
#
