# -*- coding: utf-8 -*-

import logging
import sys
import os
import subprocess
import json
import datetime

import pymongo

from zomphp_settings import BACKEND_CLASS_NAME, BACKEND_KWARGS


class BaseBackend(object):
    '''
    A base class that any backend must extend, and override its methods `record`, `likely_belongs`, and `next_func` according to specs
    Note that every worker thread has its own backend object
    '''
    def record(self, filename, function, lineno):
        '''
        Report to whatever backend lies here
        '''
        raise NotImplementedError

    def likely_belongs(self, filename, function):
        '''
        Must return True iff this backend has recorded an entry with that file name and that function name
        '''
        raise NotImplementedError

    def next_func(self, filename, lineno):
        '''
        Must return the very next (line-wise) function name recorded for that filename
        that occurs after this lineno (i.e. s.t. its line # >= lineno)
        And None if no record matches that definition
        '''
        raise NotImplementedError

    # DON'T OVERRIDE THE REMAINING FUNCTIONS

    def _function_called(self, filename, function, lineno, strict=False, translator=translator):
        '''
        Returns True if that function has been called
        '''
        if translator:
            filename = translator.translate(filename)
        if strict:
            return self.next_func(filename, lineno) == function
        else:
            return self.likely_belongs(filename, function)

    def process_raw_data(self, data):
        '''
        `data` is a string formatted in the ZomPHP usual form, i.e. path/to/file.php:funcName:lineNo
        '''
        data, _, lineno = data.rpartition(':')
        filename, _, function = data.rpartition(':')
        self.record(filename, function, lineno)

    def process_file(self, path, strict=False, translator=None):
        '''
        Parses a file and marks the unused functions as such!
        `strict` might find more false negatives, but less false positives
        Returns the real path of the file on success
        '''
        # PHP always unrolls symlinks, at least something it does right :-)
        path = os.path.realpath(path)
        logging.info('Processing file %s' % path)
        file_functions = self._get_file_functions(path)
        logging.debug('Found functions %s' % file_functions)
        if not file_functions:
            # nothing to do
            return

        current_line_nb = 0
        new_content = ''
        with open(path, 'r') as source:
            while True:
                current_line = source.readline()
                current_line_nb += 1
                if not current_line:
                    # we're done
                    break
                for function in file_functions.get(current_line_nb, []):
                    if self._function_called(path, function, current_line_nb, strict, translator=translator):
                        logging.debug('Function %s:%s:%d appears to be used' % (path, function, current_line_nb))
                    else:
                        logging.debug('Flagging %s:%s:%d as not used!' % (path, function, current_line_nb))
                        new_content += self._generate_warning(function) + '\n'
                new_content += current_line

        # let's replace the old file with the new content
        with open(path, 'w') as new_file:
            new_file.write(new_content)

        return path

    def _should_process_file(self, filename):
        '''
        Should return True iff we want to process that file
        Current policy: we process only *.php files
        '''
        return filename.endswith('.php')

    def process_directory(self, directory_path, strict=False, translator=None, ignore_sub_dirs=[]):
        for root, _, files in os.walk(directory_path):
            for rel_path in files:
                if not self._should_process_file(rel_path):
                    logging.debug('Ignoring %s file' % rel_path)
                    continue

                abs_path = os.path.join(root, rel_path)
                real_path = os.path.realpath(abs_path)
                if real_path != abs_path:
                    logging.debug('Ignoring symlinked file %s, will be processed as %s' % (abs_path, real_path))
                    continue
                for sub_dir in ignore_sub_dirs:
                    if abs_path.startswith(sub_dir):
                        logging.debug('Ignoring file %s in ignored sub-dir %s' % (abs_path, sub_dir))
                        continue

                self.process_file(abs_path, strict, translator)

    @staticmethod
    def _generate_warning(function):
        return '// ZomPHP warning : the function %s seems be be unused (%s)' % (function, datetime.datetime.now())

    @staticmethod
    def _get_file_functions(path):
        '''
        Returns the result from lib/extract_functions.php
        '''
        # ugly, but eh...
        extract_exec = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib', 'extract_functions.php')
        try:
            data = subprocess.check_output('%s "%s"' % (extract_exec, path), shell=True)
            return {int(k): v for k, v in json.loads(data).items()}
        except subprocess.CalledProcessError as ex:
            logging.error('Failed to extract functions from %s: %s' % (path, ex.output))
            return {}


class DummyBackend(BaseBackend):
    '''
    Just log what ya get (for debugging purposes only)
    '''

    def record(self, filename, function, lineno):
        logging.debug('DummyBackend received: %s:%s:%s' % (filename, fucntion, lineno))


class MongoBackend(BaseBackend):
    '''
    Just records everything in mongo
    '''

    # the key names
    _FILENAME_KEY = 'fl'
    _FUNCTION_KEY = 'fc'
    _LINENO_KEY = 'l'

    def __init__(self, db_name, col_name, size, user='', password='', **mongo_client_kwargs):
        '''
        The size is the size of the Mongo capped collection (in bytes) - should be big enough to hold the whole thing
        The last arg is passed as is to pymongo's MongoClient's constuctor
        (see http://api.mongodb.org/python/current/api/pymongo/mongo_client.html#pymongo.mongo_client.MongoClient)
        '''
        client = pymongo.MongoClient(**mongo_client_kwargs)
        if user:
            client[db_name].authenticate(user, password)
        self._create_mongo_col(client, db_name, col_name, size)
        self._mongo_col = client[db_name][col_name]
        self._ensure_index()

    @staticmethod
    def _create_mongo_col(client, db_name, col_name, size):
        '''
        Creates the right Mongo collection, if not present
        If it is present, it checks that it's got the right settings, otherwise it deletes it
        and re-creates it
        '''
        db_object = client[db_name]
        try:
            return db_object.create_collection(col_name, capped=True, size=size, autoIndexId=False)
        except pymongo.errors.CollectionInvalid:
            # the collection already exists, we check it has the right settings
            # otherwise delete it, and re-create it!
            logging.info('Checking %s.%s\'s settings' % (db_name, col_name))
            if not MongoBackend._check_coll_setings(client, db_object[col_name], size):
                logging.info('Wrong settings, dropping and re-creating collection')
                db_object.drop_collection(col_name)
                return MongoBackend._create_mongo_col(client, db_name, col_name, size)

    @staticmethod
    def _check_coll_setings(client, col_object, size):
        '''
        Returns true iff the settings are OK
        '''
        # first no autoIndexId
        for idx in col_object.index_information().values():
            if idx['key'] == [(u'_id', 1)]:
                logging.debug('Found an index on _id')
                return False
        # then that it's capped, with the right size
        options = col_object.options()
        if not options.get('capped', False):
            logging.debug('Collection not capped')
            return False
        if options.get('size', -1) != size:
            logging.debug('Capped collection does not have the right size (expected %d VS actual %d)' % (size, options.get('size', -1)))
            return False
        # all good!
        return True

    def _ensure_index(self):
        '''
        Ensures we have the right indexes on the coll
        '''
        # the main index, also OK for likely_belongs
        self._mongo_col.ensure_index([(key, pymongo.ASCENDING) for key in (self._FILENAME_KEY, self._FUNCTION_KEY, self._LINENO_KEY)], name='main_index', unique=True, dropDups=True)
        # the index used for next_func
        self._mongo_col.ensure_index([(key, pymongo.ASCENDING) for key in (self._FILENAME_KEY, self._LINENO_KEY, self._FUNCTION_KEY)], name='next_func_index')

    def record(self, filename, function, lineno):
        doc = {self._FILENAME_KEY: filename, self._FUNCTION_KEY: function, self._LINENO_KEY: int(lineno)}
        self._mongo_col.update(doc, doc, upsert=True, manipulate=False, w=0, check_keys=False)

    def likely_belongs(self, filename, function):
        return self._mongo_col.find_one({self._FILENAME_KEY: filename, self._FUNCTION_KEY: function}, fields=[]) is not None

    def next_func(self, filename, lineno):
        try:
            record = self._mongo_col.find({self._FILENAME_KEY: filename, self._LINENO_KEY: {'$gte': lineno}}, fields=[self._FUNCTION_KEY]).sort(self._LINENO_KEY).limit(1).next()
            return record[self._FUNCTION_KEY]
        except StopIteration:
            # no such record found
            return None


def get_new_backend():
    '''
    Returns a new backend object, according to the settings
    '''
    return eval(BACKEND_CLASS_NAME)(**BACKEND_KWARGS)
