from dotenv import load_dotenv
from pynput import keyboard
from pynput.keyboard import Key

import concurrent.futures
import logging
import os
import queue
import sqlalchemy
import sqlalchemy.exc
import sys


MODIFIERS = {
    Key.shift, Key.shift_l, Key.shift_r,
    Key.alt, Key.alt_l, Key.alt_r, Key.alt_gr,
    Key.ctrl, Key.ctrl_l, Key.ctrl_r,
    Key.cmd, Key.cmd_l, Key.cmd_r,
}

TABLE = sqlalchemy.Table(
    'keyboard_monitor',
    sqlalchemy.MetaData(),
    sqlalchemy.Column('hits', sqlalchemy.String),
    sqlalchemy.Column('ts', sqlalchemy.DateTime),
)


if __name__ == '__main__':
    load_dotenv()
    
    log = logging.getLogger("agent")
    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s %(message)s')
    file_handler = logging.FileHandler('agent.log', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    log.addHandler(stdout_handler)

    engine = sqlalchemy.create_engine(os.environ['DATABASE_URL'], echo_pool=True, isolation_level='AUTOCOMMIT')
    current_modifiers = set()
    pending_hits = queue.Queue()
    cancel_signal = queue.Queue()

    def on_press(key):
        if key in MODIFIERS:
            current_modifiers.add(key)
        else:
            hits = sorted([ str(key) for key in current_modifiers ]) + [ str(key) ]
            hits = '+'.join(hits)
            pending_hits.put(hits)
        log.debug(f'{key} pressed, current_modifiers: {current_modifiers}')

    def on_release(key):
        if key in MODIFIERS:
            try:
                current_modifiers.remove(key)
            except KeyError:
                log.warning(f'Key {key} not in current_modifiers {current_modifiers}')
        log.debug(f'{key} released, current_modifiers: {current_modifiers}')

    with engine.connect() as connection:
        connection.execute(sqlalchemy.sql.text("""
            CREATE TABLE IF NOT EXISTS keyboard_monitor (
                hits STRING NULL,
                ts TIMESTAMP(3) NOT NULL,
                TIME INDEX ("ts")
            ) ENGINE=mito WITH( regions = 1, ttl = '3months')
        """))

    def sender_thread():
        while True:
            with engine.connect() as connection:
                try:
                    hits = pending_hits.get()
                    if hits is None:
                        break
                    connection.execute(TABLE.insert().values(hits=hits, ts=sqlalchemy.func.now()))
                    log.info(f'sent: {hits}')
                except sqlalchemy.exc.OperationalError as e:
                    if e.connection_invalidated:
                        log.error(f'Connection invalidated: {e}')
                        pending_hits.put(hits)
                    else:
                        log.error(f'Operational error: {e}')
                        raise

    def listener_thread():
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            log.info("Listening...")
            cancel_signal.get()
            pending_hits.put(None)

    with concurrent.futures.ThreadPoolExecutor() as executor:
        sender = executor.submit(sender_thread)
        listener = executor.submit(listener_thread)
        try:
            concurrent.futures.wait([sender, listener], return_when=concurrent.futures.FIRST_EXCEPTION)
        except KeyboardInterrupt:
            log.info("Exiting Main...")
        cancel_signal.put(True)
