import os
from os import linesep as newline
import time
import re
from enum import Enum

import ptyprocess
import utils
from utils import (local_run_cmd,
                   in_search)
from utils import (ContextError,
                   CommandError,
                   PtyProcessError,
                   ExpectError,
                   TimeoutError)
from const import (local_command_timeout,
                   remote_command_timeout,
                   intershell_command_timeout,
                   ssh_timeout,
                   telnet_timeout,
                   connect_host_timeout,
                   default_connect_timeout,
                   local_shell_prompt,
                   delay_after_quit,
                   delay_after_intr,
                   host_ping_timeout,
                   bootup_watch_period,
                   bootup_watch_timeout,
                   prompt_offset_range,
                   base_serial_port,
                   session_connect_retry,
                   session_prompt_retry)


LOCAL_SHELL_PROMPT = '>>>'
PROMPT_WAIT_LOGIN = [r": {0,3}$", r"\? {0,3}$",]
PROMPT_WAIT_INPUT = [r"\$ {0,3}$", r"# {0,3}$", r"> {0,3}$",]

connect_command_patterns = [r"^telnet$", r"^ssh$", r"^connect host$",]
quit_command_patterns = [r"^quit$", r"^exit$", r"^ctrl.?(\]|x)$",]
command_errors = ['command not found',
                  'no such file or directory',
                  'Is a directory',
                  'is not recognized as an internal or external command',
                  'invalid input detected',]
error_skip_commands = ['rm', ]

intershell_info = {
    'bmc_diag': {'img_regex': r"udibmc_.*(\.stripped)?$",
                 'exit_cmd': 'exit',
                 'init_wait': 5.0,
                 'terminator': r": {0,3}$"},

    'efi_diag': {'img_regex': r"Dsh.efi$",
                 'exit_cmd': 'exit',
                 'init_wait': 3.0,
                 'terminator': r"> {0,3}$"},
    }

class LoginCases(Enum):
    """Login cases class for matching login prompt cases."""
    INPUT_WAIT_TIMEOUT = r".*timeout.*expired"                              # wait-input timeout, need to send a newline
    CONNECTION_REFUSED = 'connection refused'                               # connection is refused
    INPUT_WAIT_USERNAME = r".*login: {0,3}$|.*us(e)?r: {0,3}$"              # wait to input username
    INPUT_WAIT_PASSWORD = r".*password: {0,3}$"                             # wait to input passphrase
    INPUT_WAIT_YES_NO = r".*\(yes/no.*\)\? {0,3}$"                          # wait to input yes
    RSA_KEY_CORRUPTED = 'warning: remote host identification has changed'   # need to remove known host file
    CISCO_SOL_ENTERED = 'CISCO Serial Over LAN:'                            # mark entering Cisco SOL shell


class UCSAgentWrapper(object):
    """Basic agent class for processing UCS server test."""
    def __init__(self, command_timeout=local_command_timeout, local_prompt=local_shell_prompt, logfile=None):
        global LOCAL_SHELL_PROMPT
        LOCAL_SHELL_PROMPT = local_prompt
        self.reset_agent()
        self.command_timeout = command_timeout
        self.logfile = logfile
    
    def reset_agent(self):
        self.pty = None
        self.prompt = LOCAL_SHELL_PROMPT
        self.user = None
        self.password = None
        self.current_session = 'localshell'
        self.host = 'localhost'
        self.intershell = False
        self.serial_port_mode = False
        self.cisco_sol_mode = False
        self.pty_linesep = '\n'
        self.command_timeout = local_command_timeout
        self.session_info_chain = []
        self.session_history = []
    
#    def running_locally(self):
#        if not self.session_info_chain: return True
#        # Recover connection if Pty died unexpectedly
#        if not self.pty or not self.pty.isalive():
#            self.log(newline + newline + 'Pty Died Unexpectedly, Recovering...' + newline + newline)
#            self.close_pty()
#            to_recover = self.session_info_chain
#            recovered = []
#            retry = session_recover_retry
#            recover_done = False
#            while retry > 0 and not recover_done:
#                for conn in to_recover:
#                    cmd = conn['connect_cmd']
#                    user = conn['user']
#                    password = conn['password']
#                    timeout = conn['connect_timeout']
#                    command_timeout = conn['command_timeout']
#                    self._connect(cmd, user=user, password=password, timeout=timeout, command_timeout=command_timeout)
#                    recovered.append(conn)
#
#                if recovered == to_recover: recover_done = True
#                else: retry = retry - 1
#
#            if recover_done: self.session_info_chain = recovered
#            else: raise RecoveryError('Pty Connection Recover Failed, Recover Session Chain:' + newline + repr(to_recover))
#
#        return False
    
    @property
    def running_locally(self):
        if self.session_info_chain:
            if not self.pty or not self.pty.isalive() or self.pty.closed:
                raise PtyProcessError('Pty Died Unexpectedly. Need Recovering.')
            return False

        if self.pty:
            self.close_pty()
        return True
    
    def _s_verify_term(self, s):
        return any(in_search(p, s[-prompt_offset_range:]) for p in PROMPT_WAIT_INPUT)
    
    def _s_verify_user(self, s, user, serial_mode):
        if not user: return True        # no username to verify
        if serial_mode: return True     # serial connect doesn't require login info
        return user in s or 'IBMC-SLOT' in s
    
    def _s_verify_prompt(self, prompt):
        if prompt and '\n' not in prompt and not utils.ucs_dupsubstr_verify(prompt):
            serial_connect = self.cisco_sol_mode or self.serial_port_mode
            return self._s_verify_term(prompt) and self._s_verify_user(prompt, self.user, serial_connect)
        return False
    
    def log(self, data=''):
        byte_wrote = 0
        if self.logfile and not self.logfile.closed:
            while data:
                wb = self.logfile.write(data)
                byte_wrote += wb
                data = data[wb:]
            self.logfile.flush()
        return byte_wrote
    
    def set_pty_prompt(self, prompt=None, intershell=False):
        if not prompt:
            self.prompt = self.get_pty_prompt(intershell=intershell)
            if self.prompt == 'Unknown':
                raise ContextError('Entered unknown %s, check your command!' %('intershell' if intershell else 'shell'))
        else:
            self.prompt = prompt

        if not intershell:
            for d in reversed(self.session_info_chain):
                if d['target_host'] == self.host:
                    d['prompt'] = self.prompt

        return self.prompt
    
    def _connect(self, cmd, **kwargs):
        """connect to new shell and reset new shell prompt."""
        cmd_argv = utils.split_command_args(cmd)
        cmd_word = cmd_argv[0]
        fixed_cmd = ' '.join(cmd_argv)
        cmd_args = [x for x in cmd_argv[1:] if not (x.startswith('-') or '=' in x)]
        login_user = kwargs.get('user')
        login_password = kwargs.get('password')
        target_host = cmd_args[0][cmd_args[0].find('@')+1: ]
        telnet_port = int(cmd_args[1]) if 'telnet' in fixed_cmd and len(cmd_args) > 1 else 0
        connect_session = '%s %s' %(cmd_word, target_host) + (' %d' %(telnet_port) if telnet_port else '')
        is_serial_port_mode = True if telnet_port >= base_serial_port else False
        # connect timeout
        if 'ssh' in fixed_cmd:
            connect_timeout = kwargs['timeout'] if kwargs.get('timeout') else ssh_timeout
        elif 'telnet' in fixed_cmd:
            connect_timeout = kwargs['timeout'] if kwargs.get('timeout') else telnet_timeout
        elif 'connect host' == fixed_cmd:
            connect_timeout = connect_host_timeout
        else:
            connect_timeout = default_connect_timeout

        session_connected = False
        connect_retry = session_connect_retry
        while connect_retry > 0 and not session_connected:
            if self.running_locally:
                self.close_pty()
                self.log('%s %s' %(self.prompt, cmd) + newline)
                self.pty = ptyprocess.PtyProcess.spawn(argv=cmd_argv)
            else:
                if ('ssh' in fixed_cmd or 'telnet' in fixed_cmd) and not self.pty_ping_host(target_host):
                    raise ConnectionError('Host [%s] unaccessible from: %s' %(target_host, self.host))
                self._send_line(fixed_cmd)
            # login process
            # list the cases that login doesn't require login info, like user and password
            if fixed_cmd == 'connect host':
                nexts = PROMPT_WAIT_INPUT
            elif is_serial_port_mode:
                nexts = PROMPT_WAIT_INPUT
                self._send_all('\n')
            else:
                nexts = PROMPT_WAIT_LOGIN
            login_password_sent = False
            while True:
                out = self.read_until(nexts, connect_timeout, ignore_error=True)
                # to avoid cases when remote system doesn't need password because of RSA key
                if 'ssh' in fixed_cmd and not login_password_sent:
                    append_out = self.read_until(PROMPT_WAIT_INPUT, 0.3, ignore_error=True)
                    if append_out.strip(): out = append_out
                # match out read with cases
                if in_search(LoginCases.INPUT_WAIT_TIMEOUT.value, out):
                    self._send_line()

                elif in_search(LoginCases.RSA_KEY_CORRUPTED.value, out.lower()):
                    self.pty_rm_known_hosts()
                    break

                elif in_search(LoginCases.INPUT_WAIT_YES_NO.value, out):
                    self._send_line('yes')

                elif in_search(LoginCases.INPUT_WAIT_USERNAME.value, out):
                    if login_user:
                        self._send_line(login_user)
                    elif login_password:
                        self._send_line(login_password)
                        login_user, login_password = login_password, login_user

                    else: raise ConnectionError('Need login info to %s: %s' %(cmd_word, target_host))
                    nexts = PROMPT_WAIT_INPUT + PROMPT_WAIT_LOGIN

                elif in_search(LoginCases.INPUT_WAIT_PASSWORD.value, out):
                    if login_password:
                        self._send_line(login_password)
                    elif login_user:
                        self._send_line(login_user)
                        login_user, login_password = login_password, login_user

                    else: raise ConnectionError('Need login info to %s: %s' %(cmd_word, target_host))
                    login_password_sent = True
                    nexts = PROMPT_WAIT_INPUT

                else:
                    if out and not in_search(LoginCases.CONNECTION_REFUSED.value, out.lower()):
                        is_cisco_sol_mode = True if in_search(LoginCases.CISCO_SOL_ENTERED.value, out) else False
                        prompt_info = utils.get_prompt_line(out)
                        serial_connect = is_serial_port_mode or is_cisco_sol_mode

                        if self._s_verify_term(prompt_info) and self._s_verify_user(prompt_info, login_user, serial_connect):
                            session_connected = True

                        elif serial_connect:
                            # watch if system is booting up, only watch serial connection systems
                            t_end_watch = time.time() + bootup_watch_timeout
                            while time.time() <= t_end_watch:
                                self._send_all('\r\n')
                                boot_stream = self.read_until(PROMPT_WAIT_INPUT, bootup_watch_period, ignore_error=True)
                                if boot_stream and boot_stream.count('\n') in (1, 2) and self._s_verify_term(boot_stream):
                                    session_connected = True
                                    break

                    if not session_connected: connect_retry -= 1 # one login attempt failed
                    break   # one login attempt completed

        if session_connected:
            # Connected Successfully
            # Set pty newline for new session
            prompt_read = prompt_read_prev = None
            retry = session_prompt_retry
            while retry > 0:
                self.flush()
                self._send_all('\r\n')
                s = self.read_until(PROMPT_WAIT_INPUT, 1, ignore_error=True)
                if s and s.count('\n') in (1,2) and self._s_verify_term(s):
                    if s.count('\n') == 2: self.pty_linesep = '\n'
                    else: self.pty_linesep = '\r\n'
                    self._send_line()
                    prompt_line = self.read_until(PROMPT_WAIT_INPUT, 1, ignore_error=True)
                    if prompt_line and prompt_line.count('\n') == 1: # do post verify
                        prompt_read_prev = utils.get_prompt_line(prompt_line)
                        if 'telnet' in fixed_cmd: prompt_read_prev = utils.prompt_strip_date(prompt_read_prev)
                        break
                retry -= 1
            if retry == 0: raise ConnectionError('Prompt set linesep failed in new session: %s, line: %s' %(connect_session, s))
            # Set pty prompt for new session
            retry = session_prompt_retry
            while retry > 0:
                self.flush()
                self._send_line()
                s = self.read_until(PROMPT_WAIT_INPUT, 1, ignore_error=True)
                prompt_info = utils.get_prompt_line(s)
                # Strip dynamic datetime part of prompt for telnet session
                if 'telnet' in fixed_cmd: prompt_info = utils.prompt_strip_date(prompt_info)
                if self._s_verify_prompt(prompt_info):
                    if not prompt_read_prev:
                        prompt_read_prev = prompt_info
                        continue
                    if not prompt_read:
                        prompt_read = prompt_info
                        if prompt_read != prompt_read_prev: # do post verify
                            prompt_read_prev = prompt_read
                            prompt_read = None
                        else: break
                retry -= 1
            if retry == 0: raise ConnectionError('Prompt set failed in new session: %s, line: %s' %(connect_session, s))
            # Update agent
            self.host = target_host
            self.current_session = connect_session
            self.user = login_user
            self.password = login_password
            self.serial_port_mode = is_serial_port_mode
            self.cisco_sol_mode = is_cisco_sol_mode
            self.prompt = prompt_read
            self.command_timeout = kwargs.get('command_timeout') if kwargs.get('command_timeout') else remote_command_timeout
            session_info = {"target_host": self.host,
                            "session": self.current_session,
                            "user": self.user,
                            "password": self.password,
                            "prompt": self.prompt,
                            "serial_port_mode": self.serial_port_mode,
                            "cisco_sol_mode": self.cisco_sol_mode,
                            "pty_linesep": self.pty_linesep,
                            "command_timeout": self.command_timeout}
            self.session_info_chain.append(session_info)
            self.session_history = []

            return True
        # Connect Failed
        raise ConnectionError('%s to %s failed with 3 retry' %(cmd_word, target_host))
    
    def _trigger_intershell(self, cmd):
        was_intershell = self.intershell
        if not self.running_locally and len(cmd.split(' ')) == 1:
            exe = cmd.split(os.sep)[-1]
            for k, v in intershell_info.items():
                if re.search(v['img_regex'], cmd):
                    self.current_session = k
                    self.command_timeout = intershell_command_timeout
                    self.executable = exe
                    self.intershell = True

        if not was_intershell and self.intershell:
            self.session_history = []
            return True
        return False
    
    def _bytes(self, data):
        return utils._bytes(data)
    
    def _str(self, data):
        return utils._str(data)
    
#    def _send_all(self, data, do_read_check=True):
#        """core interface to send command."""
#        confirmed_bytes = 0
#        tosend = self._bytes(data)
#        lsend = tosend.rstrip()
#        lb = len(lsend)
#        rsend = tosend[lb:]
#        rb = len(rsend)
#        aligned_read = ((lb+8)//8)*8
#        if self.pty and not self.pty.closed:
#            t_end_send = time.time() + 5.0
#            if lsend and do_read_check: self.flush()
#            while confirmed_bytes < lb and time.time() < t_end_send:
#                bs = self.pty.write(lsend)
#                if do_read_check:
#                    recv = s = b''
#                    while recv and not s:
#                        s = self.pty.read_all_nonblocking(size=aligned_read)
#                        recv = recv + s
#                    if lsend.startswith(recv): bs = len(recv)
#                confirmed_bytes += bs
#                lsend = lsend[bs:]
#            # Assume all left bytes have been sent successfully
#            tb = confirmed_bytes + rb
#            while confirmed_bytes < tb:
#                bs = self.pty.write(rsend)
#                confirmed_bytes += bs
#                rsend = rsend[bs:]
#
#        return confirmed_bytes == tb
    
    def _send_all(self, data):
        sent = 0
        if self.pty and not self.pty.closed:
            tosend = self._bytes(data)
            while tosend:
                bs = self.pty.write(tosend)
                sent += bs
                tosend = tosend[bs:]
        return sent
    
    def _send_line(self, line=''):
        "Send one line command with linesep fixed to pty process."
        line = line.strip()
        if line: self.session_history.append(line)
        line = line + self.pty_linesep
        return self._send_all(line)
    
    def send_control(self, char):
        out = None
        out = self.pty.sendcontrol(char)
        if char.lower() == 'c':
            time.sleep(delay_after_intr)
        return out
    
    def flush(self, delaybeforeflush=0.0, close_handler=False):
        if self.pty:
            out = self._str(self.pty.read_all_nonblocking(readafterdelay=delaybeforeflush))
            if out: out = utils.strip_ansi_escape(out)
            self.log(out)

        if close_handler:
            if self.logfile and not self.logfile.closed:
                self.logfile.flush()
                self.logfile.close()
    
    def _expect(self, out, expect):
        """Expect each item in expect list, return False only if all items are found."""
        if not expect or not out: return False

        expects = expect if isinstance(expect, type([])) else [expect,]
        do_raise = False

        for exp in expects:
            pos = in_search(exp, out, do_find=True)
            if pos < 0:
                do_raise = True
                break
            out = out[pos+len(exp):]

        return do_raise
    
    def _escape(self, out, escape):
        """Escape each item in escape list, return False if any of items is found."""
        if not escape or not out: return False

        escapes = escape if isinstance(escape, type([])) else [escape,]
        do_raise = False

        for esc in escapes:
            if in_search(esc, out):
                do_raise = True
                break

        return do_raise
    
    def atomic_read(self, timeout=None, do_expect=True):
        """Atomically read command output without waiting for certain timeout,
        this method will return instantly when commands execution complete."""
        data_rd = ''
        time_interval = 0.03
        size_interval = 1024
        if timeout is None: timeout = self.command_timeout

        if timeout > 0:
            t_start = time.time()
            exp_rd_cmplt = False

            while time.time() - t_start <= timeout:
                chunk = self._str(self.pty.read_nonblocking(size_interval))
                if do_expect and not chunk:
                    data_rd = utils.strip_ansi_escape(data_rd)
                    if in_search(self.prompt, data_rd[-(len(self.prompt)+prompt_offset_range):]):
                        exp_rd_cmplt = True
                        break
                    # In some very occasional cases, command was not completely sent, while script
                    # doesn't know that, then this output will be a substring of the sending command,
                    # like, command: run UPI DISPLAY-CONFIGURATION, output: run UPI DIS
                    # A TimeoutError will be raised here, but the process is good to continue
                    if time.time() - t_start > 5.0:
                        for history in reversed(self.session_history):
                            fuzzy_match = utils.ucs_fuzzy_match(data_rd, history)
                            if fuzzy_match: self._send_line(fuzzy_match)

                data_rd = data_rd + chunk
                time.sleep(time_interval)

            self.log(data_rd)
            if do_expect and not exp_rd_cmplt:
                raise TimeoutError('Command exceeded time limit: %r sec' %(timeout),
                                   prompt=self.prompt,
                                   output=data_rd)

        return data_rd
    
    def read_until(self, until, timeout, ignore_error=False):
        """Read certain bytes within certain time interval once a time from 
        current pty process, until the 'until' string is found, timeout is
        limited in case of infinite loop."""
        data_rd = ''
        t_interval = 0.03
        s_interval = 128
        untils = until if isinstance(until, type([])) else [until,]

        if timeout > 0:
            t_end_rd = time.time() + timeout
            expected = False
            while time.time() <= t_end_rd:
                chunk = self._str(self.pty.read_nonblocking(s_interval))
                if chunk:
                    data_rd = utils.strip_ansi_escape(data_rd + chunk)
                    for un in untils:
                        if in_search(un, data_rd):
                            expected = True
                            break

                if expected: break
                time.sleep(t_interval)

            self.log(data_rd)
            if not ignore_error and not expected:
                raise TimeoutError('No %r found within timeout: %r' %(untils, timeout),
                                   prompt=self.prompt,
                                   output=data_rd)
        return data_rd
    
    def read_expect(self, timeout=None, **kwargs):
        """Read with expected strings and certain timeout, this method is specailly 
        written for UCS test, for instance, you issue a command to start some test item, 
        wait for it to compelte within certain time, and expect a list of certain patterns."""
        if not timeout: timeout = self.command_timeout

        expects = kwargs.get('expect')
        escapes = kwargs.get('escape')

        out = self.atomic_read(timeout)

        exp_raise = self._expect(out, expects)
        esc_raise = self._escape(out, escapes)
        if exp_raise or esc_raise:
            raise ExpectError('Expect failure found inside expects: %r' %(expects if exp_raise else escapes),
                              prompt=self.prompt,
                              output=out)

        return out
    
    def run_cmd(self, cmd, **kwargs):
        """Run command method, a wrapper including the process of _send_line and wait 
        and do expect read."""
        command = utils.split_command_args(cmd)[0] if cmd.strip() else cmd
        # Do connecting first
        if re.search(r"|".join(connect_command_patterns), command): return self._connect(cmd, **kwargs)
        # Handle other commands
        timeout = kwargs.get('timeout')
        expects = kwargs.get('expect')
        escapes = kwargs.get('escape')
        out = ''
        # Process Local Running Commands
        if self.running_locally:
            self.log('%s %s' %(self.prompt, cmd) + newline)
            out, err = local_run_cmd(cmd, timeout=timeout)
            self.log('%s %s' %(self.prompt, out) + newline)
            self.log('%s %s' %(self.prompt, err) + newline)

            exp_raise = self._expect(out, expects)
            esc_raise = self._escape(out, escapes)
            if exp_raise or esc_raise:
                raise ExpectError('Expect failure found inside expects: %r' %(expects if exp_raise else escapes),
                                  prompt=self.prompt,
                                  output=out)
        # Process Remote Running Commands
        else:
            self._send_line(cmd)
            # Handling Commands which triggers session prompt reseting
            prompt_set_filter = [command == 'cd',
                                 self._trigger_intershell(cmd),
                                 kwargs.get('action') == 'FIND']
            if any(prompt_set_filter): return self.set_pty_prompt(intershell=prompt_set_filter[1])

            # Handling special cases which will cause prompt reset.
            if kwargs.get('bg_run') is True:
                time.sleep(timeout if timeout and timeout > 0 else 0)
                timeout = 0  # reset timeout since we have done wait here
                self._send_line()
            out = self.read_expect(expect=expects, escape=escapes, timeout=timeout)
            if command not in error_skip_commands and any(in_search(e, out.lower()) for e in command_errors):
                raise CommandError('Invalid command in host: %s' %(self.host), output=out)

        return out
    
    def find_session_by_host(self, host):
        walk = 0
        found = -1
        while walk < len(self.session_info_chain):
            if self.session_info_chain[walk]['target_host'] == host:
                found = walk
                break
            walk += 1

        return found
    
    def find_first_telnet_session(self):
        walk = 0
        found = -1
        while walk < len(self.session_info_chain):
            if 'telnet' in self.session_info_chain[walk]['session']:
                found = walk
                break
            walk += 1

        return found
    
    def find_first_sol_session(self):
        walk = 0
        found = -1
        while walk < len(self.session_info_chain):
            if self.session_info_chain[walk]['cisco_sol_mode']:
                found = walk
                break
            walk += 1

        return found
    
    def get_pty_current_host(self):
        if self.running_locally: return 'localhost'
        out = 'unknown'
        try:
            self.flush()
            host_check_cmd = "ifconfig | awk '/inet addr/{print substr($2,6)}'"
            out = self.run_cmd(host_check_cmd)
        except:
            pass
        return out
    
    def get_pty_prompt(self, intershell=False):
        if self.running_locally: return LOCAL_SHELL_PROMPT

        if intershell:
            wait = intershell_info[self.current_session]['init_wait']
            time.sleep(wait if wait > 0 else 0)

        prompt1 = prompt2 = None
        nexts = [intershell_info[self.current_session]['terminator'],] if intershell else PROMPT_WAIT_INPUT
        retry = session_prompt_retry
        while retry > 0:
            self.flush()
            self._send_line()
            s = self.read_until(nexts, 1, ignore_error=True)
            prompt_info = utils.get_prompt_line(s)
            # This is to skip time print, [Mon Apr 13 17:34:58 root@UCSC-C240-M6SX-WZP23350BLA:/]$
            if 'telnet' in self.current_session: prompt_info = utils.prompt_strip_date(prompt_info)
            # Check prompt info and validate
            if intershell or self._s_verify_prompt(prompt_info):
                if not prompt1:
                    prompt1 = prompt_info
                    continue
                if not prompt2:
                    prompt2 = prompt_info
                    if prompt1 != prompt2: # do post verify
                        prompt1 = prompt2
                        prompt2 = None
                    else:
                        return prompt2
            retry -= 1

        return 'Unknown'
    
    def pty_ping_host(self, host):
        self.flush()
        ping_cmd = 'ping -c 2 ' + host
        out = ''
        try:
            out = self.run_cmd(ping_cmd, timeout=host_ping_timeout)
        except TimeoutError:
            self.send_control('c')

        return True if ('seq' in out and 'ttl' in out and 'time' in out) or 'alive' in out else False
    
    def pty_rm_known_hosts(self):
        rm_known_hosts_cmd = 'rm -f ~/.ssh/known_hosts'
        try:
            self.flush()
            self.run_cmd(rm_known_hosts_cmd)
        except:
            pass
    
    def pty_pulse_session(self):
        if not self.running_locally:
            pulse_cmd = "while :; do echo 'Hit CTRL+C'; sleep 240; done"
            self.run_cmd(pulse_cmd, timeout=-1)
    
    def quit(self):
        if self.running_locally:
            session_index = self.find_session_by_host(self.host)
            if session_index >= 0: del self.session_info_chain[session_index:]
            self.close_pty()
            self.reset_agent()

        elif self.intershell:
            self._send_line(intershell_info[self.current_session]['exit_cmd'])
            self.current_session = self.session_info_chain[-1]['session']
            self.prompt = self.session_info_chain[-1]['prompt']
            self.command_timeout = self.session_info_chain[-1]['command_timeout']
            self.intershell = False
            del self.executable
            time.sleep(delay_after_quit)
            self.session_history = []

        else:
            if self.serial_port_mode:
                self.send_control('c')
                self.send_control(']')
                try:
                    out = self.read_until('telnet>', telnet_timeout)
                except TimeoutError:
                    raise RuntimeError("Current session should be telnet to serial port: %s"
                                       %(self.current_session))
                self._send_line('q')
                first_telnet_index = self.find_first_telnet_session()
                if first_telnet_index >= 0: del self.session_info_chain[first_telnet_index:]

            elif self.cisco_sol_mode:
                self.send_control('x')
                first_sol_index = self.find_first_sol_session()
                if first_sol_index >= 0: del self.session_info_chain[first_sol_index:]

            else:
                self.send_control('c')
                self._send_line('exit')
                session_index = self.find_session_by_host(self.host)
                if session_index >= 0: del self.session_info_chain[session_index:]

            time.sleep(delay_after_quit)
            if self.session_info_chain:
                self.prompt = self.session_info_chain[-1]['prompt']
                self.user = self.session_info_chain[-1]['user']
                self.password = self.session_info_chain[-1]['password']
                self.host = self.session_info_chain[-1]['target_host']
                self.current_session = self.session_info_chain[-1]['session']
                self.serial_port_mode = self.session_info_chain[-1]['serial_port_mode']
                self.cisco_sol_mode = self.session_info_chain[-1]['cisco_sol_mode']
                self.pty_linesep = self.session_info_chain[-1]['pty_linesep']
                self.command_timeout = self.session_info_chain[-1]['command_timeout']
                # ???????!!!!!!!!
                host_info = self.get_pty_current_host()
                prompt_info = self.get_pty_prompt()
                if self.host not in host_info and self.prompt != prompt_info:
                    emsg = 'Enter unknown shell, host should be: %s, but read:' %(self.host) + newline + \
                        host_info + newline + 'prompt should be: %s, but read: ' %(self.prompt) + prompt_info
                    raise RuntimeError(emsg)
                self.session_history = []
            else:
                self.close_pty()
                self.reset_agent()
    
    def close_pty(self):
        if self.pty and not self.pty.closed:
            self.flush()
            msg = 'Close Pty...'
            self.log(newline + newline + msg + newline + newline)
            while not self.pty.closed:
                try:
                    self.pty.close()
                except:
                    time.sleep(0.005)

            self.reset_agent()
    
    def close_on_exception(self):
        self.log(newline + repr(self) + newline)
        self.close_pty()
        self.flush(close_handler=True)
    
    def __repr__(self):
        rpr = newline + 'Pty Session Info Dump:' + newline + newline
        rpr = rpr + 'Pty: %r' %(self.pty) + newline
        rpr = rpr + 'Host: %r' %(self.host) + newline
        rpr = rpr + 'User: %r' %(self.user if hasattr(self, 'user') and self.user else 'unknown') + newline
        rpr = rpr + 'Password: %r' %(self.password if hasattr(self, 'password') and self.password else 'unknown') + newline
        rpr = rpr + 'Session: %r' %(self.current_session if hasattr(self, 'current_session') and self.current_session else 'unknown') + newline
        rpr = rpr + 'Prompt: %r' %(self.prompt if hasattr(self, 'prompt') and self.prompt else 'unknown') + newline
        if hasattr(self, 'executable'):
            rpr = rpr + 'Executable: %r' %(self.executable) + newline
            rpr = rpr + 'Intershell: %s' %(self.intershell) + newline
        rpr = rpr + 'Cisco SOL Mode: %s' %(self.cisco_sol_mode) + newline
        rpr = rpr + 'Serial Port Mode: %s' %(self.serial_port_mode) + newline
        rpr = rpr + 'Timeout: %r' %(self.command_timeout) + newline
        rpr = rpr + 'Pty Newline: %s' %('Return + Enter' if len(self.pty_linesep)==2 else 'Enter') + newline
        return rpr
