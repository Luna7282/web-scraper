"""Local control server for the crawler -- start/stop/pause runs, read
graph projections and live status, cancel/select subtrees, export, RAG
query, all over HTTP. See server/app.py for the route implementations and
README.md for the endpoint list and the localhost-only/no-auth posture.

LOCALHOST ONLY, NO AUTH. This binds to 127.0.0.1, not 0.0.0.0 -- it is
never meant to be reachable from another machine. Do not change the host
binding without adding real authentication first; every endpoint here can
start a crawl, cancel work, or read run data with zero access control.

Flask's built-in dev server is single-threaded by default, which would
mean one slow request blocks every other request -- including a UI's
continuous status polling while a run is in flight. Everything this
server does is sub-second (a subprocess *launch* returns immediately; the
crawl itself runs elsewhere), so this is mostly precautionary, but
run(threaded=True) removes the risk entirely for the cost of one flag.
Still Werkzeug's dev server, not a production WSGI server -- fine for a
single local user, not for anything exposed beyond localhost.
"""
import argparse

from server.app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8420)
    args = parser.parse_args()

    app = create_app()
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
