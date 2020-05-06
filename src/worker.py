import sys
import os
from os import linesep as newline
import time
import datetime
import multiprocessing, logging
from multiprocessing import Process
import socket
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
                   debug_mode_on)
import utils
from utils import (ExpectError,
                   TimeoutError,
                   FileError)
from sequence import sequence_reader
import cursor

#mpl = multiprocessing.log_to_stderr()
#mpl.setLevel(logging.INFO)

class SequenceWorker(object):
    """Sequence agent worker class to run sequences, one worker corresponds
    to a specific sequence, parsed from given sequence file."""
    def __init__(self, sequence_file, loops=1, logfile=None):
        self.sequence_file = sequence_file
        self.test_sequence = sequence_reader(sequence_file)
        self.logfile = logfile
        self.test_loops = loops
        self.complt_loops = 0
        self.errordump = None
        self.agent = UCSAgentWrapper(local_prompt=local_shell_prompt, logfile=logfile)
    
    def run_item(self, cmd, **kw):
        result = Messages.ITEM_RESULT_PASS
        message = None
        try:
            self.agent.run_cmd(cmd=cmd, **kw)
        except (ExpectError, TimeoutError) as err:
            trace_msg = err.args[0] + newline + newline
            command_msg = 'Command: %s' %(cmd if cmd else 'ENTER') + newline
            session_msg = 'Running session: %s' %(self.agent.current_session) + newline
            sequence_msg = 'Sequence: %s' %(self.sequence_file) + newline
            loop_msg = 'Running loop: %d' %(self.complt_loops+1) + newline
            err_msg = trace_msg + command_msg + session_msg + sequence_msg + loop_msg
            if isinstance(err, ExpectError):
                err_to_raise = ExpectError(err_msg, prompt=err.prompt, output=err.output)
                if stop_on_failure:
                    self.errordump = err_to_raise
                    self.stop()
                    raise err_to_raise
                else:
                    result = Messages.ITEM_RESULT_FAIL
                    message = err_msg
            else:
                err_to_raise = TimeoutError(err_msg, prompt=err.prompt, output=err.output)
                self.errordump = err_to_raise
                self.stop()
                raise err_to_raise
        except Exception as err:
            self.errordump = err
            self.stop()
            raise

        return result, message
    
    def run_all(self):
        self.complt_loops = 0
        while self.complt_loops < self.test_loops:
            loop_result = Messages.LOOP_RESULT_PASS
            loop_failure_messages = []
            for command in self.test_sequence:
                if command.internal:
                    if command.action == 'INTR':
                        self.agent.send_control('c')
                        self.agent.flush()
                    elif command.action == 'QUIT':
                        self.agent.quit()
                        self.agent.flush()
                    elif command.action == 'CLOSE':
                        self.agent.close_pty()
                        self.agent.flush()
                    elif command.action == 'PULSE':
                        self.agent.pty_pulse_session()
                    elif command.action == 'WAIT':
                        seconds = utils.parse_time_to_sec(command.argv[1])
                        time.sleep(seconds if seconds > 0 else 0)
                    elif command.action == 'SET_PROMPT':
                        self.agent.set_pty_prompt(command.argv[1])
                    elif command.action == 'ENTER':
                        self.run_item('', **command.cmd_dict)
                    elif command.action == 'FIND':
                        target_found = False
                        for d in command.find_dir:
                            self.run_item(d, **command.cmd_dict)
                            if command.target_file in self.agent.run_cmd('ls', action='SEND'):
                                target_found = True
                                break
                        if not target_found:
                            self.agent.close_on_exception()
                            raise FileError('File not found: %s' %(command.target_file))
                    elif command.action == 'NEW_WORKER':
                        new_worker = Process(target=run_sequence_worker, args=(command.sequence_file,
                                                                               command.loops,))
                        new_worker.start()  # Start worker
                        sequence_name = command.sequence_file.split('.')[0]
                        ipc_message = {'MSG': Messages.SEQUENCE_START_RUNNING.value,
                                       'NAME': sequence_name,
                                       'LOOPS': command.loops}
                        self.send_ipc_msg(ipc_message)
                        #print('Spawned new worker [pid : %r] for sequence: %s' %(worker.pid, command.argv[1]))
                        # wait for derived sequence worker to complete if wait flag is set
                        if command.wait: new_worker.join()
                else:
                    result, message = self.run_item(command.command, **command.cmd_dict)
                    if result == Messages.ITEM_RESULT_FAIL:
                        loop_result = Messages.LOOP_RESULT_FAIL
                        loop_failure_messages.append(message)

            ipc_message = {'MSG': loop_result.value,
                           'NAME': self.sequence_file.split('.')[0],
                           'LOOP': self.complt_loops+1,
                           'MSG_Q': loop_failure_messages}
            self.send_ipc_msg(ipc_message)
            # move on to next loop
            self.complt_loops += 1
    
    def stop(self):
        # send COMPLETED message
        ipc_message = {'MSG': Messages.SEQUENCE_COMPLETE_RUNNING.value,
                       'NAME': self.sequence_file.split('.')[0]}
        self.send_ipc_msg(ipc_message)
        if self.logfile and not self.logfile.closed:
            self.logfile.flush()
            self.logfile.close()
            self.logfile = None

        if self.errordump:
            errordumpfile = open(utils.new_log_path(sequence=self.sequence_file.split(os.sep)[-1], suffix='errordump'), mode='w')
            error_header = '******ERROR DUMP MESSAGE******' + newline + newline
            error_body = 'TEST SEQUENCE: %s' %(self.sequence_file) + newline + newline
            error_message = error_header + error_body + repr(self.errordump) + newline
            pty_info = repr(self.agent)
            errordumpfile.write(error_message + pty_info + newline)
            errordumpfile.flush()
            errordumpfile.close()

        if self.agent:
            self.agent.close_on_exception()
            self.agent = None
    
    def send_ipc_msg(self, message):
        if debug_mode_on: return

        if not isinstance(message, str):
            try:
                message = json.dumps(message, ensure_ascii=True)
            except:
                self.stop()
                raise
        tosend = utils._bytes(message)
        t_end_ipc = time.time() + sock_retry_timeout
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ipc_msg_sent = False
        sock_connected = False
        while time.time() <= t_end_ipc:
            try:
                if not sock_connected:
                    sock.connect(unix_domain_socket)
                    sock_connected = True
            except OSError as err:
                if err.errno == 61: continue  # Connection Refused Error
                else: break
            try:
                sock.sendall(tosend)
                ipc_msg_sent = True
            except OSError as err:
                if err.errno in (9, 57): return self.send_ipc_msg(message)
                else: continue
            finally:
                sock.close()
                break

        if not ipc_msg_sent: raise RuntimeError("Worker message can't be sent: %r" %(tosend))


class Messages(Enum):
    """Signal definitions for sequence worker."""
    SEQUENCE_START_RUNNING = 1      # new sequence worker started
    SEQUENCE_COMPLETE_RUNNING = 2   # sequence worker completed
    LOOP_RESULT_PASS = 3            # one loop pass all items
    LOOP_RESULT_FAIL = 4            # one loop fail at some items
    ITEM_RESULT_PASS = 5            # one item pass
    ITEM_RESULT_FAIL = 6            # one item fail



# Sequence Worker entry, to start a worker based on a sequence file
def run_sequence_worker(sequence_file, loops=1):
    logfile = open(utils.new_log_path(sequence=sequence_file.split(os.sep)[-1]), mode='w') if log_enabled else None
    job = SequenceWorker(sequence_file=sequence_file,
                         logfile=logfile,
                         loops=loops)
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



class MasterWorker(object):
    """Master process class, for tracking statuses for all under-going test sequences."""
    def __init__(self, failure_logfile):
        self.failure_logfile = failure_logfile
        log_header = '********FAILURE LOG********' + newline + newline
        self.log(log_header)
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

        torecv = b''
        sock = None
        try:
            sock, addr = self.ipc_sock.accept()
            while True:
                data = sock.recv(2048)
                if not data:
                    if torecv:
                        try:
                            return json.loads(utils._str(torecv))
                        except ValueError:
                            torecv = b''
                            continue
                    break
                torecv = torecv + data
        except OSError as err:
            if err.errno == 35:     # Resource temporarily unavailable
                return None
            if err.errno in (9, 57):    # Broken pipe, Socket is not connected
                self.init_ipc_sock()
                return self.recv_ipc_msg()
        finally:
            if sock: sock.close()

        return None
    
    def log(self, data):
        if self.failure_logfile and not self.failure_logfile.closed:
            self.failure_logfile.write(data)
            self.failure_logfile.flush()
    
    def update_worker_status(self, msg):
        arriver = msg['NAME']
        message = msg['MSG']
        status_updated = False
        for worker in self.worker_list:
            # sequence worker has been started
            if arriver == worker['NAME']:
                if message == Messages.SEQUENCE_COMPLETE_RUNNING.value:
                    worker['STATUS'] = 'COMPLETED'
                elif message == Messages.LOOP_RESULT_FAIL.value:
                    worker['FAILURE_LOOPS'] += 1
                    worker['FAILURE_MESSAGES'].update({msg['LOOP']: msg['MSG_Q']})
                    failure_loop_log = 'FAILURE LOOP: %d' %(msg['LOOP']) + newline + 'FAILURE MESSAGES:' + newline + newline
                    for flog in msg['MSG_Q']:
                        failure_loop_log = failure_loop_log + flog + newline
                    failure_loop_log = failure_loop_log + newline
                    self.log(failure_loop_log)
                else:
                    worker['SUCCESS_LOOPS'] += 1
                status_updated = True

        if not status_updated:
            if message != Messages.SEQUENCE_START_RUNNING.value:
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
    
    def some_worker_running(self):
        return any(w['STATUS'] == 'RUNNING' for w in self.worker_list)



# ************PROGRAM MAIN ENTRY****************
def start_master(entry_sequence_file, entry_running_loops=1):
    master_failure_logfile = open(utils.new_log_path(sequence=entry_sequence_file.split(os.sep)[-1], suffix='failure'), mode='w')
    master = MasterWorker(failure_logfile=master_failure_logfile)
    # start first sequence worker
    worker = Process(target=run_sequence_worker, args=(entry_sequence_file,
                                                       entry_running_loops,))
    worker.start()
    worker_name = entry_sequence_file.split('.')[0]
    message = {'MSG': Messages.SEQUENCE_START_RUNNING.value,
               'NAME': worker_name,
               'LOOPS': entry_running_loops}
    master.update_worker_status(message)
    t_start_prog = time.time()
    master_running = True
    # window message display
    while master_running:
        ipc_message = master.recv_ipc_msg()
        if ipc_message: master.update_worker_status(ipc_message)

        window_header = newline + 'RUNNING WORKERS: %d >>>' %(len(master.worker_list)) + newline
        time_consume = str(datetime.timedelta(seconds=int(time.time()-t_start_prog)))
        window_refresh = window_header + 'TIME CONSUME: %s' %(time_consume) + newline + newline
        cursor_lines = 4
        # quit everything if all test workers finish
        if not master.some_worker_running():
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
    sys.stdout.write(newline + 'Failure log dumped into: %s' %(master_failure_logfile.name) + newline)
    sys.stdout.flush()
    if not master_failure_logfile.closed:
        master_failure_logfile.flush()
        master_failure_logfile.close()
