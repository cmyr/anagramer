from __future__ import print_function
import sqlite3 as lite
import os
import logging
import time

import utils
import twitterhandler

TWEET_DB_PATH = 'data/tweets.db'
HITS_DB_PATH = 'data/hits.db'

HIT_STATUS_REVIEW = 'review'
HIT_STATUS_REJECTED = 'rejected'
HIT_STATUS_POSTED = 'posted'
HIT_STATUS_APPROVED = 'approved'
HIT_STATUS_MISC = 'misc'
HIT_STATUS_FAILED = 'failed'


class DataHandler(object):
    """
    handles storage and retrieval of tweets
    """
    def __init__(self, just_the_hits=False):
        self.just_the_hits = just_the_hits
        self.twitterhandler = twitterhandler.TwitterHandler()
        self.data = None
        self.hitsdb = None
        self.cache = None
        self.hashes = None
        self.highest_loaded_id = 0
        self.deleted_tweets = set()
        self.setup()

    def setup(self):
        """
        creates database if it doesn't already exist
        populates hash table
        """
        if self.just_the_hits:
            # don't bother initing the cache etc
            self.hitsdb = lite.connect(HITS_DB_PATH)
            return
        if not os.path.exists(TWEET_DB_PATH):
            self.data = lite.connect(TWEET_DB_PATH)
            cursor = self.data.cursor()
            print('data not found, creating new database')
            cursor.execute("CREATE TABLE tweets(id integer, hash text, text text)")
            self.data.commit()
        else:
            self.data = lite.connect(TWEET_DB_PATH)
        if not os.path.exists(HITS_DB_PATH):
            self.hitsdb = lite.connect(HITS_DB_PATH)
            cursor = self.hitsdb.cursor()
            print('hits db not found, creating')
            cursor.execute("""CREATE TABLE hits
                (hit_id integer, hit_status text, one_id text, two_id text, one_text text, two_text text)""")
            self.hitsdb.commit()
        else:
            self.hitsdb = lite.connect(HITS_DB_PATH)
        # setup the cache
        self.load_cache()
        # setup the hashtable
        print('extracting hashes')
        cache_cursor = self.cache.cursor()
        cache_cursor.execute("SELECT hash FROM cache")
        hashes = cache_cursor.fetchall()
        self.hashes = set([str(h) for (h,) in hashes])
        print('loaded %d hashes' % (len(hashes)))

    def contains(self, tweet_hash):
        if tweet_hash in self.hashes:
            return True
        else:
            return False

    def count(self):
        cursor = self.data.cursor()
        cursor.execute("SELECT Count() FROM tweets")
        diskcount = cursor.fetchone()
        cache_cursor = self.cache.cursor()
        cache_cursor.execute("SELECT Count() FROM cache")
        cachecount = cache_cursor.fetchone()
        return (diskcount, cachecount)

    def count_hashes(self):
        return len(self.hashes)

    def add(self, tweet):
        cursor = self.cache.cursor()
        cursor.execute("INSERT INTO cache VALUES (?,?,?)", (str(tweet['id']), tweet['hash'], tweet['text']))
        self.hashes.add(tweet['hash'])
        self.cache.commit()

    def get(self, tweet_hash):
        # if hit isn't in data, check if it's still in the cache
        cache_cursor = self.cache.cursor()
        cache_cursor.execute("SELECT id, hash, text FROM cache WHERE hash=:hash",
                             {"hash": tweet_hash})
        result = cache_cursor.fetchone()
        if result:
            return {'id': long(result[0]), 'hash': str(result[1]), 'text': str(result[2])}
        return None

    def pop(self, tweet_hash):
        result = self.get(tweet_hash)
        self.remove(tweet_hash)
        return result

    def remove(self, tweet_hash):
        # add to our 'deleted list' so we can delete from disk when saving cache
        tweet = self.get(tweet_hash)
        self.deleted_tweets.add(tweet['id'])
        # delete from cache
        cache_cursor = self.cache.cursor()
        cache_cursor.execute("DELETE FROM cache WHERE hash=:hash",
                             {"hash": tweet_hash})
        self.cache.commit()
        # delete from hashes
        self.hashes.remove(tweet_hash)

    def add_hit(self, hit):
        cursor = self.hitsdb.cursor()
        cursor.execute("INSERT INTO hits VALUES (?,?,?,?,?,?)",
                      (str(hit['id']), hit['status'],
                       str(hit['tweet_one']['id']),
                       str(hit['tweet_two']['id']),
                       hit['tweet_one']['text'],
                       hit['tweet_two']['text'])
                       )
        self.hitsdb.commit()

    def get_hit(self, hit_id):
        cursor = self.hitsdb.cursor()
        cursor.execute("SELECT * FROM hits WHERE hit_id=:id",
                       {"id": str(hit_id)})
        result = cursor.fetchone()
        return self.hit_from_sql(result)

    def remove_hit(self, hit_id):
        cursor = self.hitsdb.cursor()
        cursor.execute("DELETE FROM hits WHERE hit_id=:id",
                       {"id": str(hit_id)})
        self.hitsdb.commit()

    def set_hit_status(self, hit_id, status):
        if status not in [HIT_STATUS_REVIEW, HIT_STATUS_MISC,
                          HIT_STATUS_APPROVED, HIT_STATUS_POSTED,
                          HIT_STATUS_REJECTED, HIT_STATUS_FAILED]:
            return False
        # get the hit, delete the hit, add it again with new status.
        hit = self.get_hit(hit_id)
        hit['status'] = status
        self.remove_hit(hit_id)
        self.add_hit(hit)

    def get_all_hits(self):
        cursor = self.hitsdb.cursor()
        cursor.execute("SELECT * FROM hits")
        results = cursor.fetchall()
        hits = []
        for item in results:
            hits.append(self.hit_from_sql(item))
        return hits

    def hit_from_sql(self, item):
        """
        convenience method for converting the result of an sql query
        into a python dictionary compatable with anagramer
        """
        return {'id': long(item[0]),
                'status': str(item[1]),
                'tweet_one': {'id': long(item[2]), 'text': str(item[4])},
                'tweet_two': {'id': long(item[3]), 'text': str(item[5])}
                }

    def load_cache(self):
        # load data from file into memory
        print('loading cache')
        load_time = time.time()
        cursor = self.data.cursor()
        cursor.execute("SELECT * FROM tweets")
        results = cursor.fetchall()
        self.cache = lite.connect(':memory:')
        cache_cursor = self.cache.cursor()
        cache_cursor.execute("CREATE TABLE cache(id integer, hash text, text text)")
        cache_cursor.executemany("INSERT INTO cache VALUES (?, ?, ?)", results)
        self.cache.commit()
        # now get max ID (but only if we have anything in the cache):
        if results:
            cache_cursor.execute("SELECT id from cache")
            ids = cache_cursor.fetchall()
            self.highest_loaded_id = max(ids)[0]
            print("found highest id: %i from %i ids" % (self.highest_loaded_id, len(ids)))
        load_time = time.time() - load_time
        print('loaded %i tweets to cache in %s' %
              (len(results), utils.format_seconds(load_time)))

    def write_cache(self):
        """
        write the cache to disk
        """
        print('\nwriting cache to disk')
        load_time = time.time()
        cache_cursor = self.cache.cursor()
        cache_cursor.execute("SELECT * FROM cache")
        results = cache_cursor.fetchall()
        results = [(i, h, t) for (i, h, t) in results if i > self.highest_loaded_id]
        cursor = self.data.cursor()
        cursor.executemany("INSERT INTO tweets VALUES (?, ?, ?)", results)
        # remove from disk tweets deleted from the cache:
        print('deleted %i tweets' % len(self.deleted_tweets))
        for t in self.deleted_tweets:
            cursor.execute("DELETE FROM tweets where id=:id",
                           {"id": t})
        self.data.commit()
        cache_cursor.execute("DELETE FROM cache")
        self.cache.commit()
        load_time = time.time() - load_time
        print('saved %i tweets to disk in %s' %
              (len(results), utils.format_seconds(load_time)))

    def finish(self):
        if not self.just_the_hits:
            self.write_cache()
            print('datahandler closing with %i tweets' % (self.count()[0]))
        if self.data:
            self.data.close()
        if self.cache:
            self.cache.close()
        if self.hitsdb:
            self.hitsdb.close()

    # functions for handling hit processing

    def reject_hit(self, hit_id):
        self.set_hit_status(hit_id, HIT_STATUS_REJECTED)

    def post_hit(self, hit_id):
        if self.twitterhandler.post_hit(self.get_hit(hit_id)):
            self.set_hit_status(hit_id, HIT_STATUS_POSTED)
            return True
        else:
            self.set_hit_status(hit_id, HIT_STATUS_FAILED)
            return False

    def approve_hit(self, hit_id):
        self.set_hit_status(hit_id, HIT_STATUS_APPROVED)

    def review_hits(self):
        """
        this is a simple command line tool for reviewing and categorizing
        potential anagrams as they come in. It's intended as a stopgap/
        fallback pending my adding a more elegent solution.
        """
        # should only be run with the just_the_hits flag
        if not self.just_the_hits:
            return
        hits = self.get_all_hits()
        print('db contains %i hits' % len(hits))
        hits = [h for h in hits if h['status'] in [HIT_STATUS_REVIEW]]
        print('recorded %i hits in need of review' % len(hits))
        for hit in hits:
            print(hit['tweet_one']['text'], hit['tweet_one']['id'])
            print(hit['tweet_two']['text'], hit['tweet_two']['id'])
        for hit in hits:
            print(hit['tweet_one']['id'], hit['tweet_two']['id']), hit['status']
            print(hit['tweet_one']['text'])
            print(hit['tweet_two']['text'])

            while 1:
                inp = raw_input("(a)ccept, (r)eject, (s)kip, (i)llustrate, (q)uit:")
                if inp == 'i':
                    utils.show_anagram(hit['tweet_one']['text'], hit['tweet_two']['text'])
                    continue
                if inp not in ['a', 'r', 's', 'q', 'i']:
                    print("invalid input. Please enter 'a', 'r', 's', 'i' or 'q'.")
                else:
                    break
            if inp == 'a':
                if not self.post_hit(hit['id']):
                    print('retweet failed, sorry bud')
                else:
                    print('post successful')
            if inp == 'r':
                # remove from list of hits
                self.reject_hit(hit['id'])
            if inp == 's':
                self.approve_hit(hit['id'])
            if inp == 'q':
                break

if __name__ == "__main__":
    dh = DataHandler(just_the_hits=True)
    dh.review_hits()
    dh.finish()
