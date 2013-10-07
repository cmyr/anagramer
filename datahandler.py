from __future__ import print_function
import anydbm
import multidbm
import os
import re
import sys
import logging
import time
import cPickle as pickle
import multiprocessing
from operator import itemgetter


import anagramfunctions
import hitmanager
import anagramstats as stats

from constants import (ANAGRAM_FETCH_POOL_SIZE, ANAGRAM_CACHE_SIZE,
                       STORAGE_DIRECTORY_PATH, ANAGRAM_STREAM_BUFFER_SIZE)


DATA_PATH_COMPONENT = 'anagrammdbm'
CACHE_PATH_COMPONENT = 'cachedump'

from hitmanager import (HIT_STATUS_SEEN, HIT_STATUS_REVIEW, HIT_STATUS_POSTED,
        HIT_STATUS_REJECTED, HIT_STATUS_APPROVED, HIT_STATUS_MISC,
        HIT_STATUS_FAILED)



class NeedsMaintenance(Exception):
    """
    hacky exception raised when DataCoordinator is no longer able to keep up.
    use this to signal that we should shutdown and perform maintenance.
    """
    pass


class DataCoordinator(object):
    """
    DataCoordinator handles the storage, retrieval and comparisons
    of anagram candidates.
    It caches newly returned or requested candidates to memory,
    and maintains & manages a persistent database of older candidates.
    """
    def __init__(self, languages=['en'], noload=False):
        """
        language selection is not currently implemented
        """
        self.languages = languages
        self.cache = dict()
        self.datastore = None
        self._should_trim_cache = False
        self._write_process = None
        self._lock = multiprocessing.Lock()
        self._is_writing = multiprocessing.Event()
        self.dbpath = (STORAGE_DIRECTORY_PATH +
                       DATA_PATH_COMPONENT +
                       '_'.join(self.languages) + '.db')
        self.cachepath = (STORAGE_DIRECTORY_PATH +
                          CACHE_PATH_COMPONENT +
                          '_'.join(self.languages) + '.p')
        if not noload:
            self._setup()

    def _setup(self):
        """
        - unpickle previous session's cache
        - load / init database
        - extract hashes
        """
        self.cache = self._load_cache()
        self.datastore = multidbm.MultiDBM(self.dbpath)
        hitmanager._setup(self.languages)

    def handle_input(self, tweet):
        """
        recieves a filtered tweet.
        - checks if it exists in cache
        - checks if in database
        - if yes checks for hit
        """

        key = tweet['tweet_hash']
        if key in self.cache:
            stats.cache_hit()
            hit_tweet = self.cache[key]['tweet']
            if anagramfunctions.test_anagram(tweet['tweet_text'], hit_tweet['tweet_text']):
                del self.cache[key]
                hitmanager.new_hit(tweet, hit_tweet)
            else:
                self.cache[key]['tweet'] = tweet
                self.cache[key]['hit_count'] += 1
        else:
            # not in cache. in datastore?
            if key in self.datastore:
                self._process_hit(tweet)
            else:
                # not in datastore. add to cache
                self.cache[key] = {'tweet': tweet,
                                   'hit_count': 0}
                stats.set_cache_size(len(self.cache))

                if len(self.cache) > ANAGRAM_CACHE_SIZE:
                    self._trim_cache()


    def _process_hit(self, tweet):
        key = tweet['tweet_hash']
        hit_tweet = _tweet_from_dbm(self.datastore[key])
        if anagramfunctions.test_anagram(tweet['tweet_text'],
            hit_tweet['tweet_text']):
            hitmanager.new_hit(hit_tweet, tweet)
        else:
            self.cache[key] = {'tweet': tweet, 'hit_count': 1}


    def _trim_cache(self, to_trim=None):
        """
        takes least frequently hit tweets from cache and writes to datastore
        """

        self._should_trim_cache = False
        # first just grab hashes with zero hits. If that's less then 1/2 total
        # do a more complex filter
            # find the oldest, least frequently hit items in cache:
        cache_list = self.cache.values()
        cache_list = [(x['tweet']['tweet_hash'],
                       x['tweet']['tweet_id'],
                       x['hit_count']) for x in cache_list]
        s = sorted(cache_list, key=itemgetter(1))
        cache_list = sorted(s, key=itemgetter(2))
        if not to_trim:
            to_trim = min(10000, (ANAGRAM_CACHE_SIZE/10))
        hashes_to_save = [x for (x, y, z) in cache_list[:to_trim]]

        # write those caches to disk, delete from cache, add to hashes
        for x in hashes_to_save:

            self.datastore[x] = _dbm_from_tweet(self.cache[x]['tweet'])
            del self.cache[x]

        buffer_size = stats.buffer_size()
        if buffer_size > ANAGRAM_STREAM_BUFFER_SIZE:
            self.perform_maintenance()

    def _save_cache(self):
        """
        pickles the tweets currently in the cache.
        doesn't save hit_count. we don't want to keep briefly popular
        tweets in cache indefinitely
        """
        tweets_to_save = [self.cache[t]['tweet'] for t in self.cache]
        try:
            pickle.dump(tweets_to_save, open(self.cachepath, 'wb'))
            print('saved cache to disk with %i tweets' % len(tweets_to_save))
        except:
            logging.error('unable to save cache, writing')
            self._trim_cache(len(self.cache))

    def _load_cache(self):
        print('loading cache')
        cache = dict()
        try:
            loaded_tweets = pickle.load(open(self.cachepath, 'r'))
            # print(loaded_tweets)
            for t in loaded_tweets:
                cache[t['tweet_hash']] = {'tweet': t, 'hit_count': 0}
            print('loaded %i tweets to cache' % len(cache))
            return cache
        except IOError:
            logging.error('error loading cache :(')
            return cache
            # really not tons we can do ehre


    def perform_maintenance(self):
        """
        called when we're not keeping up with input.
        moves current database elsewhere and starts again with new db
        """
        print("perform maintenance called")
        # save our current cache to be restored after we run _setup (hacky)
        moveddb = self.datastore.archive()
        print('moved mdbm chunk: %s' % moveddb)
        print('mdbm contains %s chunks' % self.datastore.section_count())

        # oldcache = self.cache
        # print('stashing cache with %i items' % len(oldcache))
        # self.close()
        # # move current db out of the way
        # newpath = (STORAGE_DIRECTORY_PATH +
        #             DATA_PATH_COMPONENT +
        #             '_'.join(self.languages) +
        #             time.strftime("%b%d%H%M") + '.db')
        # os.rename(self.dbpath, newpath)
        # self._setup()
        # print('restoring cache with %i items' % len(oldcache))
        # self.cache = oldcache


    def close(self):
        if self._write_process and self._write_process.is_alive():
            print('write process active. waiting.')
            self._write_process.join()

        self._save_cache()
        self.datastore.close()


def _tweet_from_dbm(dbm_tweet):
    tweet_values = re.split(unichr(0017), dbm_tweet.decode('utf-8'))
    t = dict()
    t['tweet_id'] = int(tweet_values[0])
    t['tweet_hash'] = tweet_values[1]
    t['tweet_text'] = tweet_values[2]
    return t


def _dbm_from_tweet(tweet):
    dbm_string = unichr(0017).join([unicode(i) for i in tweet.values()])
    return dbm_string.encode('utf-8')

def delete_short_entries(dbpath, cutoff=20):
    try:
        import gdbm
    except ImportError:
        print('database manipulation requires gdbm')

    start_time = time.time()
    db = gdbm.open(dbpath, 'w')
    k = db.firstkey()
    seen = 0
    deleted = 0
    prevk = k
    todel = set()
    try:
        while k is not None:
            seen += 1
            prevk = k
            nextk = db.nextkey(k)
            if anagramfunctions.length_from_hash(k) < cutoff:
                todel.add(k)
                deleted += 1
            sys.stdout.write('seen/deleted: %i/%i\r' % (seen, deleted))
            sys.stdout.flush()
            k = nextk
    finally:
        for i in todel:
            del db[i]
        db.close()
        duration = time.time() - start_time
        print('\ndeleted %i of %i in %s' %
            (deleted, seen, anagramfunctions.format_seconds(duration)))


def combine_databases(path1, path2, minlen=20, start=0):
    try:
        import gdbm
    except ImportError:
        print('combining databases requires the gdbm module. :(')
    print('adding tweets from %s to %s' % (path2, path1))

    db1 = gdbm.open(path1, 'w')
    db2 = gdbm.open(path2, 'w')

    k = db2.firstkey()
    temp_k = None
    seen = 0
    # if not start:
    #     start = 10**10

    if start:
        seen = 0
        while seen < start:
            k = db2.nextkey(k)
            sys.stdout.write('skipping: %i/%i \r' % (seen, start))
            sys.stdout.flush()
            seen += 1
    
    try:
        while k is not None:
            tweet = _tweet_from_dbm(db2[k])
            stats.tweets_seen()
            if len(anagramfunctions.stripped_string(tweet['tweet_text'])) < minlen:
                k = db2.nextkey(k)
                continue
            stats.passed_filter()
            if k in db1:
                tweet2 = _tweet_from_dbm(db1[k])
                if anagramfunctions.test_anagram(
                    tweet['tweet_text'],
                    tweet2['tweet_text']
                    ):
                    temp_k = db2.nextkey(k)
                    del db2[k]
                    hitmanager.new_hit(tweet, tweet2)
                else:
                    pass
            else:
                db1[k] = _dbm_from_tweet(tweet)
            stats.update_console()
            k = db2.nextkey(k)
            if not k and temp_k:
                k = temp_k
                temp_k = None
    finally:
        db1.close()
        db2.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2:
        print('please select exactly two target databases')

    start = args[2] if len(args) > 2 else None

    combine_databases(args[0], args[1], start=int(start))
    # dc = DataCoordinator()
    # sys.exit(1)
    pass
