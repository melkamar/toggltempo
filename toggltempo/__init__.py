from argparse import ArgumentParser, RawTextHelpFormatter, Namespace
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import *
import requests
import requests.auth
import yaml
import re
import datetime
import logging

CONFIG_FILE_DEFAULT_PATH = '.config/toggltempo.yaml'

DEFAULT_CONFIG_FILE = '''jira_tempo:
  api_token: ''  # Create a Tempo API token at "https://YOUR-WORKSPACE.atlassian.net/plugins/servlet/ac/io.tempo.jira/tempo-app#!/configuration/api-integration".'
  atlassian_username: ''  # The Atlassian/Jira username (e-mail) you are using
  atlassian_api_token: ''  # Create an Atlassian API token at https://id.atlassian.com/manage-profile/security/api-tokens. This is used to fetch Jira tickets with the --import command.
  jira_baseurl: ''  # the URL of your Jira instance (https://somejira.atlassian.net/)
  user_id: ''  # Find your Jira user ID by clicking your user avatar in Jira UI and going to Profile. The ID will be in the address bar.
toggl_track:
  email: ''  # Enter your e-mail and password to the Toggl Track service
  password: ''  # This is only needed if submitting data through the Toggl Track API. Leave it empty if submitting file-based data.
'''


def seconds_to_human_readable(seconds: int) -> str:
    return str(datetime.timedelta(seconds=seconds))


def time_str_to_seconds(time_str: str) -> int:
    """
    Supported formats:
      59m
      2h40m
    """
    if m := re.match(r'\s*((?P<hours>\d+)h)?\s*((?P<minutes>\d+)m)?\s*', time_str):
        hours = int(m.group('hours') or 0)
        minutes = int(m.group('minutes') or 0)
        return hours * 3600 + minutes * 60

    raise NotImplementedError(f'Unsupported time entry format: {time_str}')


@dataclass
class TempoEntry:
    date: str
    issue_key: str
    time_logged_seconds: int
    description: str

    def __repr__(self):
        return f'{self.date} | {self.issue_key:10} | {seconds_to_human_readable(self.time_logged_seconds):10} | {self.description}'


@dataclass
class Config:
    jira_tempo_user_id: str
    jira_tempo_api_token: str
    atlassian_username: str
    atlassian_api_token: str
    jira_baseurl: str
    toggl_email: str
    toggl_password: str


class ConfigNotInitializedException(Exception):
    pass


class TogglTrackApi:
    def __init__(self, toggl_email: str, toggl_password: str):
        self.toggl_email = toggl_email
        self.toggl_password = toggl_password

    def get_entries_for_date(self, date: str) -> List[TempoEntry]:
        response = requests.get(
            'https://api.track.toggl.com/api/v9/me/time_entries',
            {
                'start_date': date,
                'end_date': f'{date}T23:59:59Z'
            },
            auth=requests.auth.HTTPBasicAuth(self.toggl_email, self.toggl_password),
            headers={
                'Content-Type': 'application/json',
            }
        )
        response.raise_for_status()
        """
        Response e.g.
          {
            "id": 3190848114,
            "workspace_id": 6676428,
            "project_id": 194052205,
            "task_id": null,
            "billable": false,
            "start": "2023-11-01T15:44:33Z",
            "duration": 2264,
            "description": "fennia vcr...",
            "tags": [],
            "tag_ids": [],
            "duronly": true,
            "at": "2023-11-01T15:44:34+00:00",
            "server_deleted_at": null,
            "user_id": 8775085,
            "uid": 8775085,
            "wid": 6676428,
            "pid": 194052205
          }
        """
        _logger().debug(f'Toggl API response: {response.content}')

        result = []
        for time_entry_obj in response.json():
            _logger().info(f'Toggl API, processing item: {time_entry_obj}')
            duration = time_entry_obj['duration']
            description = time_entry_obj['description']

            if 'tags' in time_entry_obj:
                if 'nobill' in time_entry_obj['tags']:
                    print(
                        f'  - Skipping import of "{description} ({seconds_to_human_readable(duration)})", because it is tagged with #nobill')
                    continue

            # Note: project can be null if none assigned
            project_id = time_entry_obj['project_id']
            if not project_id:
                raise ValueError(
                    f'Toggl Track entry with description "{description}" does not have a project assigned. Aborting tracking. I expect that all entries have a project, from which the Jira ticket can be determined.')

            project_name = self._get_project_name_from_id(time_entry_obj['workspace_id'], project_id)
            issue_key = self._get_issue_key_from_project_name(project_name)

            result.append(
                TempoEntry(
                    date,
                    issue_key,
                    duration,
                    description
                )
            )

        return self._merge_identical_entries(result)

    def _get_project_name_from_id(self, workspace_id: int, project_id: int) -> str:
        response = requests.get(
            f'https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects/{project_id}',
            auth=requests.auth.HTTPBasicAuth(self.toggl_email, self.toggl_password),
            headers={
                'Content-Type': 'application/json',
            }
        )
        response.raise_for_status()
        return response.json()['name']

    def _get_issue_key_from_project_name(self, project_name: str) -> str:
        return project_name.split()[0]

    def _merge_identical_entries(self, tempo_entries: List[TempoEntry]) -> List[TempoEntry]:
        result = {}
        for entry in tempo_entries:
            key = f'{entry.issue_key}@@@{entry.description}'
            if key in result:
                result[key].time_logged_seconds += entry.time_logged_seconds
            else:
                result[key] = entry
        return list(result.values())

    def create_project(self, project_name: str) -> str:
        """
        Create a project in Toggl Track
        :param project_name: the full name of the project, e.g. "RH-1234 Some ticket thing"
        :return the project id
        """
        workspace_id = self._get_workspace_id_of_latest_time_entry()

        response = requests.post(
            f'https://api.track.toggl.com/api/v9/workspaces/{workspace_id}/projects',
            auth=requests.auth.HTTPBasicAuth(self.toggl_email, self.toggl_password),
            headers={
                'Content-Type': 'application/json',
            },
            json={
                "active": True,
                "is_private": False,
                "name": project_name,
            }
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            raise Exception(response.content) from e
        return response.json()['id']

    def _get_workspace_id_of_latest_time_entry(self) -> int:
        """
        Get the workspace_id value for the latest time entry. This assumes that the user only belongs to a single workspace.
        :return: workspace ID
        """
        response = requests.get(
            'https://api.track.toggl.com/api/v9/me/time_entries',
            auth=requests.auth.HTTPBasicAuth(self.toggl_email, self.toggl_password),
            headers={
                'Content-Type': 'application/json',
            }
        )
        response.raise_for_status()

        js = response.json()
        if not js:
            raise ValueError('There are zero time entries, cannot determine the workspace ID')

        return js[0]['workspace_id']


def read_report_file(report_file: Path) -> List[TempoEntry]:
    date = report_file.name
    result = []
    for line in report_file.read_text().splitlines():
        line = line.strip()
        if len(line) == 0:
            continue
        if line.startswith('#'):
            continue

        issue_key, time, description = line.split(' ', maxsplit=2)
        result.append(
            TempoEntry(
                date,
                issue_key,
                time_str_to_seconds(time),
                description
            )
        )
    return result


def read_config_file(configfile: Path) -> Config:
    if not configfile.exists():
        with configfile.open('w') as f:
            f.write(DEFAULT_CONFIG_FILE)
        raise ConfigNotInitializedException(configfile.resolve())

    with configfile.open() as f:
        yml = yaml.safe_load(f)
        try:
            return Config(
                yml['jira_tempo']['user_id'],
                yml['jira_tempo']['api_token'],
                yml['jira_tempo']['atlassian_username'],
                yml['jira_tempo']['atlassian_api_token'],
                yml['jira_tempo']['jira_baseurl'],
                yml['toggl_track']['email'],
                yml['toggl_track']['password'],
            )
        except KeyError as e:
            print(f'Could not parse config file "{configfile.resolve()}". Missing a key: {e}. Expected format:\n---\n{DEFAULT_CONFIG_FILE}', file=sys.stderr)
            raise e


def send_entries_to_tempo(date: str, entries: List[TempoEntry], config: Config):
    for entry in entries:
        issue_id = jira_issue_key_to_id(entry.issue_key, config)

        data = {
            "issueId": issue_id,
            "timeSpentSeconds": entry.time_logged_seconds,
            "startDate": date,
            "startTime": "09:00:00",
            "description": entry.description,
            "authorAccountId": config.jira_tempo_user_id
        }
        _logger().debug(data)

        response = requests.post(
            'https://api.tempo.io/4/worklogs',
            json=data,
            headers={
                'Authorization': f'Bearer {config.jira_tempo_api_token}'
            }
        )
        response.raise_for_status()

        print(f'  {entry.issue_key} ✅')

def jira_issue_key_to_id(issue_key: str, config: Config) -> int:
    response = requests.get(
        f'https://{config.jira_baseurl}/rest/api/latest/issue/{issue_key}',
        auth=requests.auth.HTTPBasicAuth(config.atlassian_username, config.atlassian_api_token)
    )
    response.raise_for_status()

    js = response.json()
    _logger().debug(js)
    return js['id']

def assert_date_format_yyyy_mm_dd(date: str):
    if not re.match(r'\d\d\d\d-\d\d-\d\d', date):
        raise ValueError(f'Given date "{date}" does not match the expected YYYY-MM-DD format.')


def parse_args():
    p = ArgumentParser(
        description='''  Send time logging data to Jira. 

  If DATE is not provided, data from the previous workday will be used. Workdays are MTWTF, no consideration is
  made for public holidays. When executed on Monday, sending data for Friday will be assumed. Otherwise, data from
  yesterday will be assumed.
  To send data for a particular DATE, use the format YYYY-MM-DD.

  When importing time entries from Toggl Track, a certain format is expected:
    1) Each time entry MUST be assigned to a Project. 
    2) The Project name MUST be in format "RH-1234 Some freetext whatever". 
       The first field of the project name (split by a whitespace) is expected to be the Jira issue ID.
       The rest of the project name is ignored.
    3) Each time entry MUST contain a text description. This description will be used as the Jira Tempo 
       worklog description.

    When tracking entries in Toggl Track, it's useful to use the "@" shortcut to add a Project to 
    the currently tracked entry.
    
    ---
    
    It is also possible to read the time entries from a plain text file with the --file option. 
    The format of the file is:
    
        # Comments
        PROJ-123  1h5m Some description that may contain spaces
        MISC-9876 5m First column is the Jira issue ID, second column is the time to log, and everything else will be the description 
''',
        formatter_class=RawTextHelpFormatter
    )
    p.add_argument('DATE', default=None, nargs='?')
    p.add_argument('-c', '--config', help=f'Path to a configuration file. Defaults to "~/{CONFIG_FILE_DEFAULT_PATH}"')
    p.add_argument('--file', action='store_true',
                   help='If provided, read input from a file. Default is to read from Toggl Track API.')
    p.add_argument('-i', '--import', dest='jiraimport', nargs=1,
                   help='Instead of logging time, import a Jira ticket as a project to Toggl Track. Requires the Jira ticket ID as an argument.')
    p.add_argument('-v', '--verbose', action='count', default=0)

    return p.parse_args()


def _read_config(args: Namespace) -> Config:
    if args.config:
        configfile = Path(args.config)
    else:
        configfile = Path.home().joinpath(CONFIG_FILE_DEFAULT_PATH)

    try:
        return read_config_file(configfile)
    except ConfigNotInitializedException as e:
        print(f'''Config file not found: "{e}" 
The configuration file has been created now. Fill in the required options there.''')
        exit(1)


def _cmd_import_jira_ticket_to_toggl(args: Namespace):
    jira_id = args.jiraimport[0]
    config = _read_config(args)

    # Get issue summary
    response = requests.get(
        f'https://{config.jira_baseurl}/rest/api/latest/issue/{jira_id}',
        auth=requests.auth.HTTPBasicAuth(config.atlassian_username, config.atlassian_api_token)
    )
    response.raise_for_status()

    js = response.json()
    summary = js['fields']['summary']
    api = TogglTrackApi(config.toggl_email, config.toggl_password)

    toggl_project_name = f'{jira_id} {summary}'.strip()

    api.create_project(toggl_project_name)
    print(f'Project created in Toggl Track:\n\n  {toggl_project_name}\n\nYou can now use it in time tracking ✅')


def _cmd_track_time(args: Namespace):
    date = args.DATE
    read_from_file = args.file
    config = _read_config(args)

    if not date:
        if read_from_file:
            print('When --file is specified, DATE must be provided')
            exit(1)

        today = datetime.datetime.now()
        print('Argument DATE not provided.')
        if today.weekday() == 0:
            last_friday = today - datetime.timedelta(days=3)
            suggested = last_friday.strftime('%Y-%m-%d')
            print(f'Assuming you want to log hours for last Friday: "{suggested}"')
        else:
            yesterday = today - datetime.timedelta(days=1)
            suggested = yesterday.strftime('%Y-%m-%d')
            print(f'Assuming you want to log hours for yesterday: "{suggested}"')

        if input(
                'Is that OK? You will be prompted again before sending any time logs, no worries. (y to confirm): ') != 'y':
            print('Aborting, goodbye.')
            return

        date = suggested

    if read_from_file:
        print(f'Reading entries from file {date}')
        report_file = Path(date)
        # The date can be a path when used with --file, use just the last part
        date = report_file.name
        assert_date_format_yyyy_mm_dd(date)
        tempo_entries = read_report_file(report_file)
    else:
        # read toggl api
        assert_date_format_yyyy_mm_dd(date)
        print('Reading entries from Toggl API')
        api = TogglTrackApi(config.toggl_email, config.toggl_password)
        tempo_entries = api.get_entries_for_date(date)

    errors = []
    print(f'Will log the following entries into date "{date}":')
    print('')
    for entry in tempo_entries:
        entry_line = f'  - {entry}'
        if entry.description.strip() == '':
            errors.append(f'Entry for {entry.issue_key} is missing a description')
            entry_line += ' ⚠️'
        print(entry_line)
    print('')

    total_seconds_logged = sum(map(lambda item: item.time_logged_seconds, tempo_entries))
    print(f'Total time: {seconds_to_human_readable(total_seconds_logged)}')
    print()

    if errors:
        print('There are some errors that prevent logging the times:')
        for error in errors:
            print(f'  - {error}')
        exit(1)

    if input('Is that OK? (y to confirm): ') != 'y':
        print('Aborting, goodbye.')
        return

    send_entries_to_tempo(date, tempo_entries, config)
    print('All sent 🎉')


def _logger():
    return logging.getLogger()


def main():
    args = parse_args()

    if args.verbose == 0:
        level = logging.CRITICAL
    elif args.verbose == 1:
        level = logging.WARNING
    elif args.verbose == 2:
        level = logging.INFO
    else:
        level = logging.DEBUG

    logging.basicConfig(
        level=level
    )

    if args.jiraimport:
        _cmd_import_jira_ticket_to_toggl(args)
    else:
        _cmd_track_time(args)


if __name__ == '__main__':
    main()
