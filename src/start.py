import os
import argparse
import const

NAME = 'UCS AutoRobot'
VERSION = '2.4.1'
AUTHOR = "Ming Li(mingli5@cisco.com)."

# feed command line arguments
parser = argparse.ArgumentParser(prog=NAME,
                                 description='Sequence designed test automation for UCS Servers.',
                                 add_help=False)
parser.add_argument('-v', '--version', action='version', version=' %(prog)s ' + VERSION,
                    help='Print %(prog)s version info and exit.')
parser.add_argument('-f', metavar='Sequence file', nargs='?', default='',
                    dest='sequence_file_entry', help='Specify entry sequence file to start test.')
parser.add_argument('-l', metavar='Loop iteration', nargs='?',
                    default=1, type=int, dest='loop',
                    help='Specify loop iterations.')
parser.add_argument('-S', '--stop-on-failure', dest='stop_on_failure',
                    action='store_true', help='set stop on failure.')
parser.add_argument('-L', '--log-enabled', dest='log_enabled',
                    action='store_true', help='enable file logging.')
parser.add_argument('-D', '--debug-mode', dest='debug_mode_on',
                    action='store_true', help='enable debug mode.')
#parser.add_argument('-P', '--print-window-message', dest='print_window_message',
#                    action='store_true', help='enable window message printing.')
parser.add_argument('-h', '--help', action='help',
                    help='Show this help information and exit.')


cmd_options = parser.parse_args()
const.sequence_file_entry = cmd_options.sequence_file_entry
const.log_enabled = cmd_options.log_enabled
const.stop_on_failure = cmd_options.stop_on_failure
const.loop_iterations = cmd_options.loop
const.debug_mode_on = cmd_options.debug_mode_on
#const.print_window_message = cmd_options.print_window_message

# check files and folders
if not os.path.isdir('./test_sequences'): os.mkdir('./test_sequences')
if not os.path.isdir('./log'): os.mkdir('./log')
if not os.path.isdir('./log/failure'): os.mkdir('./log/failure')
if not os.path.isdir('./log/errordump'): os.mkdir('./log/errordump')
if not os.path.isdir('./csvdump'): os.mkdir('./csvdump')


from worker import start_master
from worker import run_sequence_worker

if __name__ == '__main__':
    # Display tool information before launching
    print('\n%s, Version: %s' %(NAME, VERSION))
    print('Cisco UCS Server Testing Automation Tool.')
    print('Author: ' + AUTHOR)
    if const.debug_mode_on: run_sequence_worker(None, const.sequence_file_entry, loops=const.loop_iterations)
    else: start_master(const.sequence_file_entry, entry_running_loops=const.loop_iterations)
