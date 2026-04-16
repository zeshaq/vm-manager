# gunicorn.conf.py  — loaded automatically from the working directory
# Switches worker class to GeventWebSocketWorker so WebSocket connections
# (noVNC, terminal, docker exec) work correctly alongside the gevent event loop.

def on_starting(server):
    """Override the worker class before any workers are spawned.

    The systemd ExecStart uses --worker-class gevent which gets baked into
    cfg before on_starting fires.  We patch server.worker_class directly
    so all forked workers use GeventWebSocketWorker instead.
    """
    try:
        from geventwebsocket.gunicorn.workers import GeventWebSocketWorker
        server.worker_class = GeventWebSocketWorker
        server.cfg.settings['worker_class'].default = \
            'geventwebsocket.gunicorn.workers.GeventWebSocketWorker'
        print("[hypercloud] Using GeventWebSocketWorker for WebSocket support")
    except ImportError:
        print("[hypercloud] geventwebsocket not available — WebSocket may not work correctly")
