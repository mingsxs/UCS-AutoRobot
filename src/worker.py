import sys
import os
import errno
from os import linesep as newline
import time
import datetime
from multiprocessing import Process
import socket
import re
import json
from enum import Enum

from agent import UCSAgentWrapper
import const
from const import (unix_domain_socket,
                   loop_iterations,
                   stop_on_failure,
                   log_enabled,
                   local_shell_prompt,
                   sequence_file_entry,
                   max_sequences,
                   window_refresh_interval,
                   sock_retry_timeout,
                   session_recover_retry,
                   debug_mode_on)
import utils
from utils import (ExpectError,
                   TimeoutError,
                   FileError,
                   RecoveryError)
from sequence import sequence_reader
import cursor

#mpl = multiprocessing.log_to_stderr()
#mpl.setLevel(logging.INFO)

#SEQUENCE WORKER
class SequenceWorker(object):
    """Sequence agent worker class to run sequences, one worker corresponds
    to a specific sequence, parsed from given sequence file."""
    def __init__(self, sequence_file, loops=1):
        self.sequence_file = sequence_file
        self.test_sequence = sequence_reader(sequence_file)
        self.logfile = open(utils.new_log_path(sequence=sequence_file.split(os.sep)[-1]), mode='w') if log_enabled else None
        self.errordumpfile = None
        self.test_loops = loops
        self.complt_loops = 0
        self.agent = UCSAgentWrapper(local_prompt=local_shell_prompt, logfile=self.logfile)
        self.errordump = None
        self.spawned_workers= []
    
    def log_error(self, errorinfo):
        if not self.errordumpfile:
            error_header = '******ERROR DUMP MESSAGE******' + newline + newline
            error_title = 'TEST SEQUENCE: %s' %(self.sequence_file) + newline + newline
            self.errordumpfile = open(utils.new_log_path(sequence=self.sequence_file.split(os.sep)[-1], suffix='errordump'), mode='w')
            self.errordumpfile.write(error_header + error_title)
            self.errordumpfile.flush()

        if self.errordumpfile and not self.errordumpfile.closed:
            if isinstance(errorinfo, Exception): errorinfo = repr(errorinfo)
            self.errordumpfile.write(errorinfo)
            self.errordumpfile.flush()
    
    def run_item(self, cmd, **kw):
        result = Messages.ITEM_RESULT_PASS
        message = None
        output = ''
        try:
            output = self.agent.run_cmd(cmd=cmd, **kw)
        # This is the main entry for handling Worker/Agent Errors.
        except Exception as err:
            trace_msg = err.args[0] + newline
            command_msg = 'Command: %s' %(cmd if cmd else 'ENTER') + newline
            session_msg = 'Running session: %s' %(self.agent.current_session) + newline
            sequence_msg = 'Sequence: %s' %(self.sequence_file) + newline
            loop_msg = 'Running loop: %d' %(self.complt_loops+1) + newline
            err_msg = trace_msg + command_msg + session_msg + sequence_msg + loop_msg
            # Handling Expect Errors
            if isinstance(err, ExpectError):
                err_to_raise = ExpectError(err_msg, prompt=err.prompt, output=err.output)
                if stop_on_failure:
                    self.errordump = err_to_raise
                    self.stop()
                    raise err_to_raise
                else:
                    result = Messages.ITEM_RESULT_FAIL
                    message = err_msg
            # Handling Timeout Errors, should be really dangerous.
            elif isinstance(err, TimeoutError):
                err_to_raise = TimeoutError(err_msg, prompt=err.prompt, output=err.output)
                self.errordump = err_to_raise
                self.stop()
                raise err_to_raise
            # Handling Unknown errors
            else:
                self.errordump = err
                self.log_error('ERROR INFO:' + newline + repr(err))
                self.log_error(err_msg + newline)
                agent_info = 'AGENT INFO:' + newline + repr(self.agent)
                self.log_error(agent_info + newline)
                result = Messages.ITEM_RESULT_UNKNOWN

        return result, message, output
    
    def run_all(self):
        total = len(self.test_sequence)
        last_recover_loop = 0
        recover_retry = session_recover_retry
        self.complt_loops = 0
        while self.complt_loops < self.test_loops:
            loop_result = Messages.LOOP_RESULT_PASS
            loop_failure_messages = []
            current = 0
            self.spawned_workers = []
            while current < total:
                command = self.test_sequence[current]
                if command.internal:
                    if command.action == 'INTR':
                        try:
                            self.agent.send_control('c')
                        except Exception as err:
                            self.log_error(repr(err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]
                        self.agent.flush()

                    elif command.action == 'QUIT':
                        try:
                            self.agent.quit()
                        except Exception as err:
                            self.log_error(repr(err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]
                        self.agent.flush()

                    elif command.action == 'CLOSE':
                        self.agent.close_pty()
                        self.agent.flush()

                    elif command.action == 'PULSE':
                        try:
                            self.agent.pty_pulse_session()
                        except Exception as err:
                            self.log_error(repr(err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]

                    elif command.action == 'WAIT':
                        seconds = utils.parse_time_to_sec(command.argv[1])
                        time.sleep(seconds)

                    elif command.action == 'SET_PROMPT':
                        try:
                            self.agent.set_pty_prompt(command.argv[1])
                        except Exception as err:
                            self.log_error(repr(err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]

                    elif command.action == 'ENTER':
                        self.run_item('', **command.cmd_dict)

                    elif command.action == 'FIND':
                        target_found = False
                        outputs = []
                        for d in command.find_dir:
                            if not re.search(r"^FS\d+:$", d.strip()) and 'cd' not in d: d = 'cd ' + d
                            self.run_item(d, **command.cmd_dict)
                            result, message, output = self.run_item('ls', action='SEND')
                            outputs.append(output)
                            if utils.in_search(command.target_file, output):
                                target_found = True
                                break
                        if not target_found:
                            ferr = FileError('File not found: %s' %(command.target_file))
                            self.log_error(repr(ferr) + newline + newline + repr(outputs) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(ferr), repr(outputs)]
                            if self.errordump: loop_failure_messages.append(repr(self.errordump))

                    elif command.action == 'NEW_WORKER':
                        new_worker = Process(target=run_sequence_worker, args=(command.sequence_file,
                                                                               command.loops,))
                        #if command.wait: new_worker.daemon = True
                        new_worker.start()  # Start worker
                        ipc_message = {'MSG': Messages.SEQUENCE_RUNNING_START.value,
                                       'NAME': command.sequence_file.split('.')[0],
                                       'LOOPS': command.loops}
                        self.send_ipc_msg(ipc_message)
                        #print('Spawned new worker [pid : %r] for sequence: %s' %(worker.pid, command.argv[1]))
                        # wait for derived sequence worker to complete if wait flag is set
                        if command.wait: new_worker.join()
                        self.spawned_workers.append(new_worker)

                else:
                    result, message, output = self.run_item(command.command, **command.cmd_dict)
                    if result == Messages.ITEM_RESULT_UNKNOWN:
                        loop_result = Messages.LOOP_RESULT_UNKNOWN
                        loop_failure_messages = [repr(self.errordump)]
                    elif result == Messages.ITEM_RESULT_FAIL:
                        loop_result = Messages.LOOP_RESULT_FAIL
                        loop_failure_messages.append(message)
                # reset loop environments, restart current loop from sequence begining
                if loop_result == Messages.LOOP_RESULT_UNKNOWN:
                    if recover_retry == 0:
                        err = RecoveryError('Recovery failed after %d time retry at loop %d' %(session_recover_retry,
                                                                                               self.complt_loops+1))
                        self.log_error(repr(err) + newline + newline)
                        self.stop()
                        time.sleep(5)
                        return

                    if self.complt_loops+1 == last_recover_loop:
                        recover_retry = recover_retry - 1
                    else:
                        last_recover_loop = self.complt_loops + 1
                        recover_retry = session_recover_retry

                    ipc_message = {'MSG': loop_result.value,
                                   'NAME': self.sequence_file.split('.')[0],
                                   'LOOP': self.complt_loops+1,
                                   'MSG_Q': loop_failure_messages}
                    self.send_ipc_msg(ipc_message)
                    # reset loop scope variables
                    for worker in self.spawned_workers:
                        worker.kill()
                        time.sleep(0.1)
                    self.agent.close_pty()
                    loop_result = Messages.LOOP_RESULT_PASS
                    loop_failure_messages = []
                    self.spawned_workers = []
                    current = 0
                else:
                    current += 1

            ipc_message = {'MSG': loop_result.value,
                           'NAME': self.sequence_file.split('.')[0],
                           'LOOP': self.complt_loops+1,
                           'MSG_Q': loop_failure_messages}
            self.send_ipc_msg(ipc_message)
            # move on to next loop
            self.complt_loops += 1
    
    def stop(self):
        # send COMPLETED message
        ipc_message = {'MSG': Messages.SEQUENCE_RUNNING_COMPLETE.value,
                       'NAME': self.sequence_file.split('.')[0]}
        self.send_ipc_msg(ipc_message)

        if self.errordump:
            error_info = 'ERROR INFO:' + newline + repr(self.errordump) + newline
            pty_info = 'AGENT INFO:' + newline + repr(self.agent) + newline
            self.log_error(error_info + newline + pty_info + newline)

        if self.agent:
            self.agent.close_on_exception()
            self.agent = None

        if self.logfile and not self.logfile.closed:
            self.logfile.flush()
            self.logfile.close()
            self.logfile = None

        if self.errordumpfile and not self.errordumpfile.closed:
            self.errordumpfile.flush()
            self.errordumpfile.close()
            self.errordumpfile = None
    
    def send_ipc_msg(self, message):
        if debug_mode_on: return

        if not isinstance(message, str): message = json.dumps(message, ensure_ascii=True)

        tosend = utils._bytes(message)
        if not tosend: return

        t_end_ipc = time.time() + sock_retry_timeout
        ipc_msg_sent = False
        while time.time() <= t_end_ipc and not ipc_msg_sent:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.setblocking(False)
                sock.connect(unix_domain_socket)
                sock.sendall(tosend)
                ipc_msg_sent = True
            except OSError as err:
                # socket connected failed or resource not available
                if err.errno not in (errno.EAGAIN, errno.EWOULDBLOCK,
                                     errno.ECONNREFUSED, errno.ECONNABORTED,
                                     errno.EBADF, errno.ENOTCONN, errno.EPIPE):
                    break
            finally:
                sock.close()

        if not ipc_msg_sent: raise RuntimeError("Worker message can't be sent: %r" %(tosend))


class Messages(Enum):
    """Signal definitions for sequence worker."""
    SEQUENCE_RUNNING_START = 1      # new sequence worker started
    SEQUENCE_RUNNING_COMPLETE = 2   # sequence worker completed
    LOOP_RESULT_UNKNOWN = 3         # one loop failed because of unknown errors
    LOOP_RESULT_PASS = 4            # one loop pass all items
    LOOP_RESULT_FAIL = 5            # one loop fail at some items
    ITEM_RESULT_UNKNOWN = 6         # one item fail because of unknown errors
    ITEM_RESULT_PASS = 7            # one item pass
    ITEM_RESULT_FAIL = 8            # one item fail


# Sequence Worker entry, to start a worker based on a sequence file
def run_sequence_worker(sequence_file, loops=1):
    job = SequenceWorker(sequence_file=sequence_file, loops=loops)
    if job.logfile and not job.logfile.closed:
        line = '*************THIS IS %s SEQUENCE LOG***************' %('MASTER' if sequence_file == sequence_file_entry else 'SLAVE')
        job.logfile.write(line + newline + newline)
        line = 'Sequence File: %s' %(sequence_file)
        job.logfile.write(line + newline + newline)
    
    #print(newline + '------Sequence Worker Started------' + newline)
    #print('Worker sequence file: %s' %(sequence_file))
    #print('Worker sequence:')
    #for command in job.test_sequence:
    #    print('\t' + repr(command))
    #print('Worker logfile: %s' %(logfile.name if logfile else 'Log Disabled'))
    #print('Total loops: %d' %(job.iterations))
    job.run_all()
    
    #print('Worker exit normally, sequence file: %s' %(sequence_file) + \
    #      (', log dumped into: %s' %(logfile.name) if logfile else ''))
    if job.logfile and not job.logfile.closed:
        line = 'Test sequence completed successfully.'
        job.logfile.write(newline + line + newline)
    job.stop()


# MASTER WORKER
class MasterWorker(object):
    """Master process class, for tracking statuses for all under-going test sequences."""
    def __init__(self, init_sequence_file):
        self.init_sequence_file = init_sequence_file
        self.failure_logfile = None
        self.worker_list = []
        self.ipc_sock = None
        self.init_ipc_sock()
    
    def init_ipc_sock(self):
        if debug_mode_on: return

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)     # set nonblocking recv requests
        try:
            os.remove(unix_domain_socket)
        except OSError:
            pass
        sock.bind(unix_domain_socket)
        sock.listen(max_sequences)
        self.ipc_sock = sock
    
    def recv_ipc_msg(self):
        if debug_mode_on: return None
        if not self.ipc_sock: self.init_ipc_sock()

        recved = b''
        message = None
        try:
            conn, addr = self.ipc_sock.accept()
            try:
                while True:
                    try:
                        s = conn.recv(4096)
                    except OSError:
                        s = b''
                    if not s and recved:
                        try:
                            message = json.loads(utils._str(recved))
                        except ValueError:
                            message = utils._str(recved)
                        break
                    recved = recved + s
            finally:
                conn.close()
        except OSError as err:
            # resource temporarily unavailable
            if err.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                self.init_ipc_sock()

        return message
    
    def log_error(self, data):
        if not self.failure_logfile:
            log_header = '********FAILURE LOG********' + newline + newline
            self.failure_logfile = open(utils.new_log_path(sequence=self.init_sequence_file.split(os.sep)[-1], suffix='failure'), mode='w')
            self.failure_logfile.write(log_header)
            self.failure_logfile.flush()

        if self.failure_logfile and not self.failure_logfile.closed:
            self.failure_logfile.write(data)
            self.failure_logfile.flush()
    
    def update_worker_status(self, msg):
        if not isinstance(msg, dict) or 'NAME' not in msg or 'MSG' not in msg: return False

        arriver = msg['NAME']
        message = msg['MSG']
        status_updated = False
        for worker in self.worker_list:
            # sequence worker has been started
            if arriver == worker['NAME']:
                if message == Messages.SEQUENCE_RUNNING_COMPLETE.value:
                    worker['STATUS'] = 'COMPLETED'
                elif message == Messages.LOOP_RESULT_UNKNOWN.value:
                    error_log = newline + 'ERROR LOOP: %d' %(msg['LOOP']) + newline + 'ERROR MESSAGES:' + newline + newline
                    for elog in msg['MSG_Q']:
                        error_log = error_log + elog + newline
                    self.log_error(error_log)
                elif message == Messages.LOOP_RESULT_FAIL.value:
                    worker['FAILURE_LOOPS'] += 1
                    worker['FAILURE_MESSAGES'].update({msg['LOOP']: msg['MSG_Q']})
                    failure_loop_log = newline + 'FAILURE LOOP: %d' %(msg['LOOP']) + newline + 'FAILURE MESSAGES:' + newline + newline
                    for flog in msg['MSG_Q']:
                        failure_loop_log = failure_loop_log + flog + newline
                    self.log_error(failure_loop_log)
                else:
                    worker['SUCCESS_LOOPS'] += 1

                status_updated = True

        if not status_updated:
            if message != Messages.SEQUENCE_RUNNING_START.value:
                raise RuntimeError('Invalid worker message received: %d' %(message))

            if len(self.worker_list) >= max_sequences:
                raise RuntimeError('Too many sequences started, maximum: %d' %(max_sequences))

            worker = {'NAME': arriver,
                      'FAILURE_LOOPS': 0,
                      'SUCCESS_LOOPS': 0,
                      'TOTAL_LOOPS': msg['LOOPS'],
                      'FAILURE_MESSAGES': {},
                      'STATUS': 'RUNNING'}
            self.worker_list.append(worker)

        return True
    
    def some_worker_running(self):
        return any(w['STATUS'] == 'RUNNING' for w in self.worker_list)


# ************PROGRAM MAIN ENTRY****************
def start_master(entry_sequence_file, entry_running_loops=1):
    master = MasterWorker(init_sequence_file=entry_sequence_file)
    # start first sequence worker
    worker = Process(target=run_sequence_worker, args=(entry_sequence_file,
                                                       entry_running_loops,))
    #worker.daemon = True
    worker.start()
    worker_name = entry_sequence_file.split('.')[0]
    message = {'MSG': Messages.SEQUENCE_RUNNING_START.value,
               'NAME': worker_name,
               'LOOPS': entry_running_loops}
    master.update_worker_status(message)
    t_start_prog = time.time()
    master_running = True
    # window message display
    while master_running:
        master.update_worker_status(master.recv_ipc_msg())

        window_header = newline + 'RUNNING WORKERS: %d >>>' %(len(master.worker_list)) + newline
        time_consume = str(datetime.timedelta(seconds=int(time.time()-t_start_prog)))
        window_refresh = window_header + 'TIME CONSUME: %s' %(time_consume) + newline + newline
        cursor_lines = 4
        # quit everything if all test workers finish
        if not master.some_worker_running():
            # handle all messages in buffer
            while master.update_worker_status(master.recv_ipc_msg()):
                pass
            if master.ipc_sock: master.ipc_sock.close()
            master_running = False

        # update window display
        for worker in master.worker_list:
            success_loops = worker['SUCCESS_LOOPS']
            failure_loops = worker['FAILURE_LOOPS']
            window_refresh = window_refresh + \
                '* Worker [%s]: %d total loops, %d loops PASS, %d loops FAIL ...' %(worker['NAME'],
                                                                                    worker['TOTAL_LOOPS'],
                                                                                    success_loops,
                                                                                    failure_loops) + newline
            cursor_lines += 1

        # print window display lines
        sys.stdout.write(window_refresh)
        sys.stdout.flush()
        # to avoid to much system cost
        if master_running:
            time.sleep(window_refresh_interval)
            cursor.erase_lines_upward(cursor_lines)

    window_summary_display = newline + 'RESULT SUMMARY:' + newline + newline
    for worker in master.worker_list:
        success_loops = worker['SUCCESS_LOOPS']
        failure_loops = worker['FAILURE_LOOPS']
        window_summary_display = newline + window_summary_display + \
            '* Sequence [%s]>> Total loops: %d, %d loops PASSED, %d loops FAILED' %(worker['NAME'],
                                                                                     success_loops+failure_loops,
                                                                                     success_loops,
                                                                                     failure_loops) + newline
        if worker['FAILURE_MESSAGES']:
            window_summary_display += 'FAILURE LOOPS: '
            window_summary_display += ', '.join([str(x) for x in worker['FAILURE_MESSAGES'].keys()])
        window_summary_display += newline

    sys.stdout.write(window_summary_display)
    sys.stdout.write(newline)
    sys.stdout.write('Failure log dumped to: %s' %(master.failure_logfile.name if master.failure_logfile else 'NONE'))
    sys.stdout.write(newline)
    sys.stdout.flush()
    if master.failure_logfile and not master.failure_logfile.closed:
        master.failure_logfile.flush()
        master.failure_logfile.close()
