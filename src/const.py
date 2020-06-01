# consts definitions from command line arguments
debug_mode_on = False                       # if debug mode enabled
log_enabled = True                          # if log is enabled
stop_on_failure = False                     # if test stops when failure is detected
loop_iterations = 1                         # test loop iterations
sequence_file_entry = ''                    # entry sequence file, the sequence to start all tests

#print_window_message = True
local_shell_prompt = '>>>'                  # local shell prompt string
session_connect_retry = 3                   # session connect retry count
session_recover_retry = 3                   # session recover retry count
session_prompt_retry = 4                    # session prompt set/get retry count
max_sequences = 5                           # maximum worker processes
window_refresh_interval = 5.0               # time period for refreshing window result printing
prompt_offset_range = 10                    # offset range to check if prompt string is reached
base_serial_port = 2003                     # base serial port for telnet connection

# sequence related definitions
seq_continue_nextline = "\\"                # sequence line syntax for continue in newline
seq_comment_header = '#'                    # sequence line syntax for commenting
seq_item_delimiter = ';'                    # sequence line syntax for spliting items
seq_subitem_delimiter = ','                 # sequence line syntax for spliting subitems

# timeout definitions
ssh_timeout = 30.0                          # default ssh connect timeout
telnet_timeout = 20.0                       # default telnet connect timeout
connect_host_timeout = 5.0                  # internal timeout const
default_connect_timeout = 15.0              # internal timeout const
local_command_timeout = 15.0                # timeout for command which is running in local shell
remote_command_timeout = 30.0               # timeout for command which is running in pty connection
intershell_command_timeout = 60.0           # timeout for command which is running inside intershell
host_ping_timeout = 6.0                     # internal timeout const

delay_after_quit = 1.0                      # internal delay const
delay_after_intr = 0.2                      # internal delay const

bootup_watch_period = 30.0                  # watch period to watch if target system is booting up
bootup_watch_timeout = 600.0                # timeout for watching system booting up

unix_domain_socket = './.uds.sock'           # unix domain socket namespace address for IPC
sock_retry_timeout = 60.0                   # retry timeout for socket IPC
