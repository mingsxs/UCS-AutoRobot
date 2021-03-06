import sys
#import traceback
import os
import errno
from os import linesep as newline
import time
import datetime
from multiprocessing import Process, Value
import socket
import re
import json
from enum import Enum

from agent import UCSAgentWrapper
import const
from const import (loop_iterations,
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
import sequence
from sequence import sequence_reader
from builtin import BuiltinCommand
import cursor

#mpl = multiprocessing.log_to_stderr()
#mpl.setLevel(logging.INFO)

UNIX_DOMAIN_SOCKET = None

#SEQUENCE WORKER
class SequenceWorker(object):
    """Sequence agent worker class to run sequences, one worker corresponds
    to a specific sequence, parsed from given sequence file."""
    def __init__(self, global_display_control, sequence_file, loops=1):
        self.sequence_file = sequence_file
        self.logfile = open(utils.new_log_path(sequence=sequence_file.split(os.sep)[-1]), mode='w') if log_enabled else None
        self.display_control = global_display_control
        self.errordumpfile = None
        self.test_loops = loops
        self.complt_loops = 0
        self.errordump = None
        self.spawned_workers= []
        self.agent = UCSAgentWrapper(local_prompt=local_shell_prompt, logfile=self.logfile)
        try:
            self.test_sequence = sequence_reader(sequence_file)
        except Exception as err:
            self.stop_display_refresh()
            self.error_logging(err)
            raise err
    
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
                sock.connect(UNIX_DOMAIN_SOCKET)
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

        if not ipc_msg_sent:
            self.stop_display_refresh()
            error = RuntimeError("Worker message can't be sent: %r" %(tosend))
            self.error_logging(error)
            raise error
    
    def error_logging(self, errorinfo):
        if not self.errordumpfile:
            error_header = '******ERROR DUMP MESSAGE******' + newline + newline
            error_title = 'TEST SEQUENCE: %s' %(self.sequence_file) + newline + newline
            self.errordumpfile = open(utils.new_log_path(sequence=self.sequence_file.split(os.sep)[-1], suffix='errordump'), mode='w')
            self.errordumpfile.write(error_header + error_title)
            self.errordumpfile.flush()

        if self.errordumpfile and not self.errordumpfile.closed:
            if not isinstance(errorinfo, str): errorinfo = repr(errorinfo)
            self.errordumpfile.write(errorinfo)
            self.errordumpfile.flush()
    
    def format_error_message(self, cmd, error):
        trace_msg = (error.args[0] if error.args else 'Null Traceback Message') + newline
        if not isinstance(error, (ExpectError, TimeoutError)):
            trace_msg = '%s: ' %(error.__class__.__name__) + trace_msg
        command_msg = 'Command: %s' %(cmd if cmd else 'ENTER') + newline
        session_msg = 'Session: %s' %(self.agent.current_session) + newline
        sequence_msg = 'Sequence: %s' %(self.sequence_file) + newline
        loop_msg = 'Loop: %d' %(self.complt_loops+1) + newline
        emsg = trace_msg + command_msg + session_msg + sequence_msg + loop_msg
        return emsg
    
    def run_sequence_command(self, command):
        result = Messages.ITEM_RESULT_PASS
        message = None
        output = ''
        try:
            output = self.agent.run_cmd(**command.cmd_dict)

        # This is the main entry for handling Worker/Agent Errors.
        except Exception as err:
            err_msg = self.format_error_message(command.command, err)
            err_to_raise = None
            # Handling Expect Errors
            if isinstance(err, ExpectError):
                if stop_on_failure:
                    err_to_raise = ExpectError(err_msg, prompt=err.prompt, output=err.output)
                else:
                    result = Messages.ITEM_RESULT_FAIL
                    message = err_msg
            # Handling Timeout Errors, should be really dangerous.
            elif isinstance(err, TimeoutError):
                err_to_raise = TimeoutError(err_msg, prompt=err.prompt, output=err.output)
            # Handling Unknown errors
            else:
                self.errordump = err
                self.error_logging(newline + 'UNKNOWN ERROR INFO:' + newline)
                #self.error_logging(traceback.format_exc())
                #self.error_logging(sys.exc_info()[2])
                self.error_logging(err_msg + newline)
                agent_info = 'AGENT INFO:' + newline + repr(self.agent)
                self.error_logging(agent_info + newline)
                result = Messages.ITEM_RESULT_UNKNOWN
            # Need to stop and raise process error
            if err_to_raise:
                ipc_message = {'MSG': Messages.LOOP_RESULT_FAIL.value,
                               'NAME': self.sequence_file.split('.')[0],
                               'LOOP': self.complt_loops+1,
                               'MSG_Q': [err_msg,]}
                self.send_ipc_msg(ipc_message)
                self.errordump = err_to_raise
                self.stop()
                raise err_to_raise

        return result, message, output
    
    def run_all(self):
        total = len(self.test_sequence)
        last_recover_loop = 0
        test_recover_retry = session_recover_retry
        self.complt_loops = 0
        while self.complt_loops < self.test_loops:
            loop_result = Messages.LOOP_RESULT_PASS
            loop_failure_messages = []
            current = 0
            self.spawned_workers = []
            while current < total:
                command = self.test_sequence[current]
                if command.builtin:
                    if command.action == 'INTR':
                        try:
                            self.agent.send_control('c')
                        except Exception as err:
                            self.error_logging(self.format_error_message('INTR', err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]
                        self.agent.flush()

                    elif command.action == 'QUIT':
                        try:
                            self.agent.quit()
                        except Exception as err:
                            self.error_logging(self.format_error_message('QUIT', err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]
                        self.agent.flush()

                    elif command.action == 'CLOSE':
                        self.agent.close_pty()

                    elif command.action == 'PULSE':
                        try:
                            self.agent.pty_pulse_session()
                        except Exception as err:
                            self.error_logging(self.format_error_message('PULSE', err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]

                    elif command.action == 'WAIT':
                        seconds = utils.parse_time_to_sec(command.argv[1])
                        time.sleep(seconds)

                    elif command.action == 'SET_PROMPT':
                        try:
                            self.agent.set_pty_prompt(command.argv[1])
                        except Exception as err:
                            self.error_logging(self.format_error_message('SET PROMPT', err) + newline + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(err)]

                    elif command.action == 'ENTER':
                        command.command = ''
                        self.run_sequence_command(command)

                    elif command.action == 'FIND':
                        target_found = False
                        outputs = []
                        for d in command.find_dir:
                            if 'cd' in d or re.search(r"^FS\d+:$", d.strip()): command.command = d
                            else: command.command = 'cd ' + d
                            self.run_sequence_command(command)
                            result, message, output = self.run_sequence_command(BuiltinCommand(action='SEND',
                                                                                               command='ls'))
                            outputs.append(output)
                            if utils.in_search(command.target_file, output):
                                target_found = True
                                break
                        if not target_found:
                            ferr = FileError('File not found: %s' %(command.target_file), outputs=outputs)
                            self.error_logging(repr(ferr) + newline)
                            loop_result = Messages.LOOP_RESULT_UNKNOWN
                            loop_failure_messages = [repr(ferr), ]
                            if self.errordump: loop_failure_messages.append(repr(self.errordump))

                    elif command.action == 'NEW_WORKER':
                        new_worker = Process(target=run_sequence_worker, args=(self.display_control,
                                                                               command.sequence_file,
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

                    elif command.action == 'MONITOR':
                        cont = True
                        while cont:
                            result, message, output = self.run_sequence_command(command)
                            for w in command.watch:
                                if w in output:
                                    cont = False
                                    break
                            if cont: time.sleep(command.interval if command.interval > 0 else 0)

                    elif command.action == 'LOOP':
                        start = sequence.SUBSEQUENCES[command.subsequence_name]['start']
                        end = sequence.SUBSEQUENCES[command.subsequence_name]['end']
                        test_loops_save = self.test_loops
                        complt_loops_save = self.complt_loops
                        test_sequence_save = self.test_sequence
                        sequence_file_save = self.sequence_file
                        self.sequence_file = command.subsequence_name
                        self.test_loops = command.loops
                        self.test_sequence = self.test_sequence[start:end]
                        ipc_message = {'MSG': Messages.SEQUENCE_RUNNING_START.value,
                                       'NAME': self.sequence_file.split('.')[0],
                                       'LOOPS': command.loops}
                        self.send_ipc_msg(ipc_message)
                        self.run_all()
                        ipc_message = {'MSG': Messages.SEQUENCE_RUNNING_COMPLETE.value,
                                       'NAME': self.sequence_file.split('.')[0]}
                        self.send_ipc_msg(ipc_message)
                        self.test_loops = test_loops_save
                        self.test_sequence = test_sequence_save
                        self.complt_loops = complt_loops_save
                        self.sequence_file = sequence_file_save

                else:
                    result, message, output = self.run_sequence_command(command)
                    if result == Messages.ITEM_RESULT_UNKNOWN:
                        loop_result = Messages.LOOP_RESULT_UNKNOWN
                        loop_failure_messages = [repr(self.errordump)]
                    elif result == Messages.ITEM_RESULT_FAIL:
                        loop_result = Messages.LOOP_RESULT_FAIL
                        loop_failure_messages.append(message)
                # reset loop environments, restart current loop from sequence begining
                if loop_result == Messages.LOOP_RESULT_UNKNOWN:
                    # DO LOOP RECOVERY
                    if test_recover_retry == 0:
                        err = RecoveryError('Recovery failed after %d retry at loop %d' %(session_recover_retry, self.complt_loops+1))
                        self.error_logging(newline + '****************ERROR DUMP END****************' + newline)
                        err_msg = newline + repr(err) + newline
                        self.error_logging(err_msg + newline)
                        self.stop()
                        time.sleep(5)
                        return
                    if (self.complt_loops + 1) == last_recover_loop:
                        test_recover_retry -= 1
                    else:
                        last_recover_loop = self.complt_loops + 1
                        test_recover_retry = session_recover_retry

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
            #self.agent.close_pty()
            self.complt_loops += 1
    
    def stop_display_refresh(self):
        if self.display_control is not None:
            self.display_control.value = 0
    
    def stop(self):
        # send COMPLETED message
        ipc_message = {'MSG': Messages.SEQUENCE_RUNNING_COMPLETE.value,
                       'NAME': self.sequence_file.split('.')[0]}
        self.send_ipc_msg(ipc_message)

        if self.errordump:
            error_info = newline + 'DUMP ERROR INFO:' + newline + repr(self.errordump) + newline
            pty_info = 'AGENT INFO:' + newline + repr(self.agent) + newline
            self.error_logging(error_info + newline + pty_info + newline)
            self.errordump = None

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
def run_sequence_worker(global_display_control, sequence_file, loops):
    job = SequenceWorker(global_display_control=global_display_control, sequence_file=sequence_file, loops=loops)
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
class Master(object):
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
            os.remove(UNIX_DOMAIN_SOCKET)
        except OSError:
            pass
        sock.bind(UNIX_DOMAIN_SOCKET)
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
    
    def failure_logging(self, data):
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
                    error_log = newline + 'ERROR LOOP: %d' %(msg['LOOP']) + newline + 'ERROR MESSAGES:' + newline
                    for elog in msg['MSG_Q']:
                        error_log = error_log + elog + newline
                    self.failure_logging(error_log)
                elif message == Messages.LOOP_RESULT_FAIL.value:
                    worker['FAILURE_LOOPS'] += 1
                    worker['FAILURE_MESSAGES'].update({msg['LOOP']: msg['MSG_Q']})
                    failure_loop_log = newline + 'FAILURE LOOP: %d' %(msg['LOOP']) + newline + 'FAILURE MESSAGES:' + newline + newline
                    for flog in msg['MSG_Q']:
                        failure_loop_log = failure_loop_log + flog + newline
                    self.failure_logging(failure_loop_log)
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
    
    @property
    def some_worker_running(self):
        return any(w['STATUS'] == 'RUNNING' for w in self.worker_list)


# ************PROGRAM MAIN ENTRY****************
def start_master(entry_sequence_file, entry_running_loops=1):
    global UNIX_DOMAIN_SOCKET
    UNIX_DOMAIN_SOCKET = utils.new_uds_name(entry_sequence_file)
    # A shared memory variable, a window display controller which can be set by all workers.
    global_display_control = Value('b', 1)
    master = Master(init_sequence_file=entry_sequence_file)
    # start first sequence worker
    worker = Process(target=run_sequence_worker, args=(global_display_control,
                                                       entry_sequence_file,
                                                       entry_running_loops,))
    #worker.daemon = True
    worker.start()
    worker_name = entry_sequence_file.split('.')[0]
    message = {'MSG': Messages.SEQUENCE_RUNNING_START.value,
               'NAME': worker_name,
               'LOOPS': entry_running_loops}
    master.update_worker_status(message)
    t_start_prog = time.time()
    # window message display
    while global_display_control.value > 0:
        master.update_worker_status(master.recv_ipc_msg())

        window_header = newline + newline + 'RUNNING WORKERS: %d ' %(len(master.worker_list)) + newline
        time_consume = str(datetime.timedelta(seconds=int(time.time()-t_start_prog)))
        window_display = window_header + 'TIME CONSUME: %s' %(time_consume) + newline + newline
        cursor_lines = 5
        # quit everything if all test workers finish
        if not master.some_worker_running:
            # handle all messages in buffer
            while master.update_worker_status(master.recv_ipc_msg()):
                pass
            if master.ipc_sock: master.ipc_sock.close()
            global_display_control.value = 0

        # update window display
        for worker in master.worker_list:
            success_loops = worker['SUCCESS_LOOPS']
            failure_loops = worker['FAILURE_LOOPS']
            window_display = window_display + \
                '* Worker [%s]: %d total loops, %d loops PASS, %d loops FAIL ...' %(worker['NAME'],
                                                                                    worker['TOTAL_LOOPS'],
                                                                                    success_loops,
                                                                                    failure_loops) + newline
            cursor_lines += 1

        # print window display lines
        sys.stdout.write(window_display)
        sys.stdout.flush()
        time.sleep(window_refresh_interval)
        # to avoid to much system cost
        if global_display_control.value > 0:
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
    sys.stdout.write(newline + newline)
    sys.stdout.flush()
    if master.failure_logfile and not master.failure_logfile.closed:
        master.failure_logfile.flush()
        master.failure_logfile.close()
    # Remove unix domain sock file
    if UNIX_DOMAIN_SOCKET: os.remove(UNIX_DOMAIN_SOCKET)

