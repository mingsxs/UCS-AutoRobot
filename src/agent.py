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
                   ExpectError,
                   CommandError,
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
                   delay_after_close,
                   delay_after_enter,
                   delay_after_intr,
                   host_ping_timeout,
                   bootup_watch_period,
                   bootup_watch_timeout,
                   prompt_offset_range,
                   base_serial_port,
                   connect_retry)


LOCAL_SHELL_PROMPT = 'Unknown'
PROMPT_WAIT_LOGIN = [r": ?$", r".*\(yes/no.*\)\? ?$",]
PROMPT_WAIT_INPUT = [r"\$ {0,2}$", r"# {0,2}$", r"> {0,2}$",]

connect_command_patterns = [r"^telnet$", r"^ssh$", r"^connect host$",]
quit_command_patterns = [r"^quit$", r"^exit$", r"^ctrl.?(\]|x)$",]
error_skip_commands = ['rm', ]
command_errors = ['command not found',
                  'no such file or directory',
                  'invalid input detected',]
intershell_triggers = {
    'bmc_diag': (r"udibmc_.*(\.stripped)?$", 'exit', 5.0),
    'uefi_diag': (r"Dsh.efi$", 'exit', 3.0),
}


class LoginCases(Enum):
    """Login cases class for matching login prompt cases."""
    INPUT_WAIT_TIMEOUT = r".*timeout.*expired"                              # wait-input timeout, need to send a newline
    CONNECTION_REFUSED = 'Connection refused'                               # connection is refused
    INPUT_WAIT_USERNAME = r".*login: ?$|.*us(e)?r: ?$"                      # wait to input username
    INPUT_WAIT_PASSWORD = r".*password: ?$"                                 # wait to input passphrase
    INPUT_WAIT_YES_NO = r".*\(yes/no.*\)\? ?$"                              # wait to input yes
    RSA_KEY_CORRUPTED = 'warning: remote host identification has changed'   # need to remove known host file
    CISCO_SOL_SIGN = 'CISCO Serial Over LAN:'                               # mark entering Cisco SOL shell


class UCSAgentWrapper(object):
    """Basic agent class for processing UCS server test."""
    def __init__(self, command_timeout=local_command_timeout, local_prompt=local_shell_prompt, logfile=None):
        global LOCAL_SHELL_PROMPT
        self.pty = None
        self.command_timeout = command_timeout
        self.prompt = local_prompt
        self.logfile = logfile
        self.session_info_chain = []
        self.current_session = 'localshell'
        self.user = None
        self.password = None
        self.host = 'localhost'
        self.intershell = False
        self.serial_port_mode = False
        self.cisco_sol_mode = False
        self.pty_linesep = '\n'
        LOCAL_SHELL_PROMPT = local_prompt
    
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
    
    def is_local(self):
        return False if self.pty and self.pty.isalive() else True
    
    def is_pty_normal_shell(self, prompt):
        if prompt and any(in_search(p, prompt[-(len(p)+prompt_offset_range):]) for p in PROMPT_WAIT_INPUT):
            return (in_search(self.user, prompt) or in_search('IBMC-SLOT', prompt)) if self.user and not self.cisco_sol_mode else True
        return False
    
    def log(self, data=''):
        w_bytes = 0
        if data and self.logfile and not self.logfile.closed:
            w_bytes = self.logfile.write(data)
            self.logfile.flush()
        return w_bytes
    
    def set_pty_prompt(self, prompt=None, intershell=False):
        if prompt and prompt.strip():
            self.prompt = prompt
        else:
            self.prompt = self.get_pty_prompt(intershell=intershell)
            if not self.prompt or self.prompt == 'Unknown':
                self.close_on_exception()
                raise ContextError('Entered unknown intershell, check your command!')

        if not intershell:
            for d in reversed(self.session_info_chain):
                if d['target_host'] == self.host:
                    d['prompt'] = self.prompt

        return self.prompt
    
    def _connect(self, cmd, **kwargs):
        """connect to new shell and reset shell prompt."""
        cmd_argv = utils.split_command_args(cmd)
        cmd_word = cmd_argv[0]
        fix_cmd = ' '.join(cmd_argv)
        cmd_args = [x for x in cmd_argv[1:] if not (x.startswith('-') or '=' in x)]
        login_user = kwargs.get('user')
        login_password = kwargs.get('password')
        target_host = cmd_args[0][cmd_args[0].find('@')+1: ]
        telnet_port = int(cmd_args[1]) if 'telnet' in fix_cmd and len(cmd_args) > 1 else 0
        connect_session = '%s %s' %(cmd_word, target_host) + (' %d' %(telnet_port) if telnet_port else '')
        is_serial_port_mode = True if telnet_port >= base_serial_port else False
        # connect timeout
        if 'ssh' in fix_cmd:
            connect_timeout = kwargs['timeout'] if 'timeout' in kwargs and kwargs['timeout'] else ssh_timeout
        elif 'telnet' in fix_cmd:
            connect_timeout = kwargs['timeout'] if 'timeout' in kwargs and kwargs['timeout'] else telnet_timeout
        elif 'connect host' == fix_cmd:
            connect_timeout = connect_host_timeout
        else:
            connect_timeout = default_connect_timeout

        target_session_connected = False
        retries = connect_retry
        while retries > 0 and not target_session_connected:
            if not self.session_info_chain:
                self.close_pty()
                self.log('%s %s' %(self.prompt, cmd) + newline)
                self.pty = ptyprocess.PtyProcess.spawn(argv=cmd_argv)
            else:
                if ('ssh' in fix_cmd or 'telnet' in fix_cmd) and not self.pty_ping_host(target_host):
                    self.close_on_exception()
                    raise ConnectionError('Host [%s] unaccessible from: %s' %(target_host, self.host))
                self._send_line(fix_cmd)
            # login process
            # list the cases that login doesn't require login info, like user and password
            if fix_cmd == 'connect host' or \
                    is_serial_port_mode:
                next_until = PROMPT_WAIT_INPUT
            else:
                next_until = PROMPT_WAIT_LOGIN
            if is_serial_port_mode: self.pty.write(self._bytes('\n'))
            login_password_sent = False
            while True:
                out = self.read_until(next_until, connect_timeout, ignore_error=True)
                # to avoid cases when remote system doesn't need password because of RSA key
                if 'ssh' in fix_cmd and not login_password_sent:
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
                    else:
                        self.close_on_exception()
                        raise ConnectionError('Need login info to %s: %s' %(cmd_word, target_host))
                    next_until = PROMPT_WAIT_INPUT + PROMPT_WAIT_LOGIN

                elif in_search(LoginCases.INPUT_WAIT_PASSWORD.value, out):
                    if login_password:
                        self._send_line(login_password)
                    elif login_user:
                        self._send_line(login_user)
                        login_user, login_password = login_password, login_user
                    else:
                        self.close_on_exception()
                        raise ConnectionError('Need login info to %s: %s' %(cmd_word, target_host))
                    login_password_sent = True
                    next_until = PROMPT_WAIT_INPUT

                else:
                    if out and not in_search(LoginCases.CONNECTION_REFUSED.value, out):
                        is_cisco_sol_mode = True if in_search(LoginCases.CISCO_SOL_SIGN.value, out) else False
                        prompt_detect = utils.get_last_line(out)

                        if any(in_search(p, prompt_detect[-(len(p)+prompt_offset_range):]) for p in PROMPT_WAIT_INPUT) and \
                                ((in_search(login_user, prompt_detect) or in_search('IBMC-SLOT', prompt_detect)) if login_user and not is_cisco_sol_mode else True):
                            target_session_connected = True
                        elif is_cisco_sol_mode or is_serial_port_mode:
                            # watch if system is booting up, only watch serial connection systems
                            t_end_watch = time.time() + bootup_watch_timeout
                            while time.time() <= t_end_watch:
                                self.pty.write(self._bytes('\r\n'))
                                boot_stream = self.read_until(PROMPT_WAIT_INPUT, bootup_watch_period, ignore_error=True)
                                if boot_stream and boot_stream.count('\n') in (1, 2) and \
                                        any(in_search(p, boot_stream[-(len(p)+prompt_offset_range):]) for p in PROMPT_WAIT_INPUT):
                                    target_session_connected = True
                                    break

                    if not target_session_connected: retries -= 1 # one login attempt failed

                    break   # one login attempt completed

        if target_session_connected:
            # Connect Successfully
            # Reset pty newline for newly connected session
            self.flush()
            self.pty.write(self._bytes('\r\n'))
            out = self.read_until('WxWxWx', delay_after_enter, ignore_error=True)
            l = [out.count(p) for p in ('$', '#', '>') if out.count(p) > 0]
            if l and all(x in (1, 2) for x in l):
                if l[0] == 1:
                    self.pty_linesep = '\r\n'
                    prompt_read = out.strip()
                else:
                    self.pty_linesep = '\n'
                    self._send_line()
                    prompt_read = self.read_until(PROMPT_WAIT_INPUT, connect_timeout).strip()
            # Strip dynamic datetime part of prompt for telnet session
            if 'telnet' in fix_cmd: prompt_read = ' '.join(prompt_read.split(' ')[4:])
            # Update agent
            self.host = target_host
            self.current_session = connect_session
            self.user = login_user
            self.password = login_password
            self.serial_port_mode = is_serial_port_mode
            self.cisco_sol_mode = is_cisco_sol_mode
            self.prompt = prompt_read
            self.command_timeout = remote_command_timeout
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
        else:
            # Connect Failed
            self.close_on_exception()
            raise ConnectionError('%s to %s failed with 3 retry' %(cmd_word, target_host))
    
    def _trigger_intershell(self, cmd):
        was_intershell = self.intershell
        if self.session_info_chain and len(cmd.split(' ')) == 1:
            exe = cmd.split(os.sep)[-1]
            for k, v in intershell_triggers.items():
                if re.search(v[0], cmd):
                    self.current_session = k
                    self.command_timeout = intershell_command_timeout
                    self.executable = exe
                    self.intershell = True

        return True if not was_intershell and self.intershell else False
    
    def _bytes(self, data):
        return utils._bytes(data)
    
    def _str(self, data):
        return utils._str(data)
    
    def _send_line(self, line=''):
        "Send line data with linesep fixed to pty process."
        line = line.strip()
        if line:
            line = line + self.pty_linesep
        else:
            line = self.pty_linesep
        try:
            return self.pty.write(self._bytes(line))
        except Exception:
            self.close_on_exception()
            raise
    
    def send_control(self, char):
        if self.pty:
            out = self.pty.sendcontrol(char)
            if char.lower() == 'c':
                time.sleep(delay_after_intr)
            return out
    
    def flush(self, delaybeforeflush=0.0, close_handler=False):
        if self.pty:
            out = self._str(self.pty.read_all_nonblocking(delaybeforeflush))
            if out: out = utils.strip_ansi_escape(out)
            self.log(out)
        if close_handler:
            if self.logfile and not self.logfile.closed:
                self.logfile.flush()
                self.logfile.close()
    
    def _expect(self, out, expect):
        """Expect each item in expect list, return False only if all items are found."""
        if not expect: return False

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
        if not escape: return False

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
        output = ''
        time_interval = 0.03
        size_interval = 1024
        if timeout is None: timeout = self.command_timeout

        if timeout > 0:
            t_end_rd = time.time() + timeout
            exp_rd_cmplt = False

            while time.time() <= t_end_rd:
                rd_data = self._str(self.pty.read_nonblocking(size_interval))
                if do_expect and not rd_data:
                    output = utils.strip_ansi_escape(output)
                    if in_search(self.prompt, output[-(len(self.prompt)+prompt_offset_range):]):
                        exp_rd_cmplt = True
                        break
                output = output + rd_data
                time.sleep(time_interval)

            self.log(output)
            if do_expect and not exp_rd_cmplt:
                raise TimeoutError('Command exceeded time limit: %r sec' %(timeout),
                                   prompt=self.prompt,
                                   output=output)

        return output
    
    def read_until(self, until, timeout, ignore_error=False):
        """Read certain bytes within certain time interval once a time from 
        current pty process, until the 'until' string is found, timeout is
        limited in case of infinite loop."""
        output = ''
        t_interval = 0.03
        s_interval = 128
        untils = until if isinstance(until, type([])) else [until,]

        if timeout > 0:
            t_end_rd = time.time() + timeout
            expected = False

            while time.time() <= t_end_rd:
                rd_data = self._str(self.pty.read_nonblocking(s_interval))
                if rd_data:
                    output = utils.strip_ansi_escape(output + rd_data)
                    for un in untils:
                        if in_search(un, output):
                            expected = True
                            break
                if expected: break
                time.sleep(t_interval)
            self.log(output)
            if not ignore_error and not expected:
                raise TimeoutError('No %r found within timeout: %r' %(untils, timeout),
                                   prompt=self.prompt,
                                   output=output)

        return output
    
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
        command = utils.split_command_args(cmd)[0] if cmd else cmd
        if re.search(r"|".join(connect_command_patterns), command):
            self._connect(cmd, **kwargs)
            return True

        intershell_triggered = self._trigger_intershell(cmd)
        set_pty_prompt_filter = [command == 'cd',
                                 intershell_triggered,
                                 kwargs.get('action') == 'FIND',]

        timeout = kwargs.get('timeout')
        expects = kwargs.get('expect')
        escapes = kwargs.get('escape')
        out = ''

        if self.is_local():
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
        else:
            if kwargs.get('action') == 'FIND' and not re.search(r"^FS\d+:$", cmd.strip()) and 'cd' not in cmd:
                cmd = 'cd ' + cmd
            self._send_line(cmd)
            # handle special cases which will cause prompt reset.
            if any(set_pty_prompt_filter):
                return self.set_pty_prompt(intershell=intershell_triggered)
            if kwargs.get('bg_run') is True:
                time.sleep(timeout if timeout and timeout > 0 else 0)
                timeout = 0  # reset timeout since we have done wait here
                self._send_line()
            out = self.read_expect(expect=expects, escape=escapes, timeout=timeout)
            if command not in error_skip_commands and any(in_search(e, out.lower()) for e in command_errors):
                self.close_on_exception()
                raise CommandError('Invalid command [%s] in host: %s' %(cmd, self.host))
            self.flush()

        return out
    
    def find_session(self, host):
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
        if self.session_info_chain:
            host_check_cmd = "ifconfig | awk '/inet addr/{print substr($2,6)}'"
            self._send_line(host_check_cmd)
            out = self.atomic_read()
            return out
        else:
            return 'localhost' if self.is_local() else 'Unknown'
    
    def get_pty_prompt(self, intershell=False):
        if self.is_local():
            return 'Unknown' if self.session_info_chain else LOCAL_SHELL_PROMPT

        if intershell:
            time.sleep(intershell_triggers[self.current_session][2])

        retry = 3
        pty_prompt = 'Unknown'
        while retry > 0 and pty_prompt == 'Unknown':
            self.flush()
            self._send_line()
            out = self.read_until('WxWxWx', delay_after_enter, ignore_error=True)
            pty_prompt = utils.get_last_line(out)
            if 'telnet' in self.current_session:
                # This is to skip time print, [Mon Apr 13 17:34:58 root@UCSC-C240-M6SX-WZP23350BLA:/]$
                pty_prompt = ' '.join(pty_prompt.split(' ')[4:])
            if not intershell and not self.is_pty_normal_shell(pty_prompt):
                pty_prompt = 'Unknown'
                out = ''
            retry -= 1

        return pty_prompt
    
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
            self.run_cmd(rm_known_hosts_cmd)
        except Exception:
            pass
    
    def pty_pulse_session(self):
        pulse_cmd = "while :; do echo 'Hit CTRL+C'; sleep 240; done"
        self._send_line(pulse_cmd)
    
    def quit(self):
        if self.is_local():
            session_index = self.find_session(self.host)
            if session_index >= 0: del self.session_info_chain[session_index:]
            self.close_pty()
            self.reset_agent()

        elif self.intershell:
            self._send_line(intershell_triggers[self.current_session][1])
            self.current_session = self.session_info_chain[-1]['session']
            self.prompt = self.session_info_chain[-1]['prompt']
            self.command_timeout = self.session_info_chain[-1]['command_timeout']
            self.intershell = False
            del self.executable
            time.sleep(delay_after_quit)

        else:
            if self.serial_port_mode:
                self.send_control('c')
                self.send_control(']')
                try:
                    out = self.read_until('telnet>', telnet_timeout)
                except TimeoutError:
                    self.close_on_exception()
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
                session_index = self.find_session(self.host)
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
                if self.host not in self.get_pty_current_host() and self.prompt != self.get_pty_prompt():
                    self.close_on_exception()
                    raise RuntimeError('Enter unknown shell! current host should be: %s, prompt: %s' %(self.host, self.prompt))
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
                except Exception:
                    time.sleep(0.005)

            self.reset_agent()
            time.sleep(delay_after_close)
    
    def close_on_exception(self):
        self.log(newline + repr(self) + newline)
        # print(repr(self))
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
        rpr = rpr + 'Pty Newline: %s' %('Return + Enter' if len(self.pty_linesep)==2 else 'Enter')
        return rpr
