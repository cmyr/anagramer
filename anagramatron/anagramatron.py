# coding: utf-8
from __future__ import print_function

import sys
import time
import multiprocessing
from datetime import datetime

from . import twitterhandler, stream, anagramfinder, hit_server, hitmanager
from .anagramstats import StatTracker


def run(server_only=False, **kwargs):
    try:
        import setproctitle
        setproctitle.setproctitle('anagramatron')
    except ImportError:
        print("missing module: setproctitle", file=sys.stderr)
        pass

    dbpath = 'hitdata3en.db'
    if server_only:
        hit_server.start_hit_server(dbpath)
    else:
        hitserver = multiprocessing.Process(target=hit_server.start_hit_server, args=[dbpath])
        hitserver.daemon = True
        hitserver.start()

        hit_manager = hitmanager.HitDBManager(dbpath)

        def handle_hit(p1, p2):
            hit_manager.new_hit(p1, p2)

        anagram_finder = anagramfinder.AnagramFinder(storage='mdbm', hit_callback=handle_hit)
        stats = StatTracker()
        while 1:
            try:
                print('starting stream handler', file=sys.stderr)
                stream_handler = stream.StreamHandler(**kwargs)
                stream_handler.start()
                for processed_tweet in stream_handler:
                    anagram_finder.handle_input(processed_tweet)
                    stats.print_stats()

            except anagramfinder.NeedsMaintenance:
                print('performing maintenance', file=sys.stderr)
                stream_handler.close()
                anagram_finder.perform_maintenance()

            except KeyboardInterrupt:
                stream_handler.close()
                anagram_finder.close()
                return 0
            except Exception as err:
                stream_handler.close()
                anagram_finder.close()
                if hasattr(err, 'code') and err.code == 503:
                    time.sleep(60*5)
                    continue
                else:
                    print('sending twitter notification for err:', err)
                    twitterhandler.TwitterHandler().send_message(
                        "%s\n%s" % (err, datetime.today().isoformat()))
                    raise


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-s', '--server-only',
        help="run in server mode only",
        action="store_true")
    parser.add_argument('--host', help="hostname for stream connection")
    parser.add_argument('--port', help="port for stream connection", type=int, default=8069)
    args = parser.parse_args()

    return run(**vars(args))


if __name__ == "__main__":
    main()
