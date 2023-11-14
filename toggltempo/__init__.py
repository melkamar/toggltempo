from argparse import ArgumentParser, RawTextHelpFormatter
from pathlib import Path
from dataclasses import dataclass
from typing import *
import requests
import requests.auth
import yaml
import re
import datetime

CONFIG_FILE_DEFAULT_PATH = '.config/toggltempo.yaml'


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
    issue_id: str
    time_logged_seconds: int
    description: str

    def __repr__(self):
        return f'{self.date} | {self.issue_id:10} | {seconds_to_human_readable(self.time_logged_seconds):10} | {self.description}'


@dataclass
class Config:
    jira_tempo_user_id: str
    jira_tempo_api_token: str
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
            "start": "2023-11-01T15:06:49+00:00",
            "stop": "2023-11-01T15:44:33Z",
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

        result = []
        for time_entry_obj in response.json():
            duration = time_entry_obj['duration']
            description = time_entry_obj['description']

            # Note: project can be null if none assigned
            project_id = time_entry_obj['project_id']
            if not project_id:
                raise ValueError(
                    f'Toggl Track entry with description "{description}" does not have a project assigned. Aborting tracking. I expect that all entries have a project, from which the Jira ticket can be determined.')

            project_name = self._get_project_name_from_id(time_entry_obj['workspace_id'], project_id)
            issue_id = self._get_issue_id_from_project_name(project_name)

            result.append(
                TempoEntry(
                    date,
                    issue_id,
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

    def _get_issue_id_from_project_name(self, project_name: str) -> str:
        return project_name.split()[0]

    def _merge_identical_entries(self, tempo_entries: List[TempoEntry]) -> List[TempoEntry]:
        result = {}
        for entry in tempo_entries:
            key = f'{entry.issue_id}@@@{entry.description}'
            if key in result:
                result[key].time_logged_seconds += entry.time_logged_seconds
            else:
                result[key] = entry
        return list(result.values())


def read_report_file(report_file: Path) -> List[TempoEntry]:
    date = report_file.name
    result = []
    for line in report_file.read_text().splitlines():
        line = line.strip()
        if len(line) == 0:
            continue
        if line.startswith('#'):
            continue

        issue_id, time, description = line.split(' ', maxsplit=2)
        result.append(
            TempoEntry(
                date,
                issue_id,
                time_str_to_seconds(time),
                description
            )
        )
    return result


def read_config_file(configfile: Path) -> Config:
    if not configfile.exists():
        with configfile.open('w') as f:
            yaml.dump(
                {
                    'jira_tempo': {
                        'user_id': '',
                        'api_token': ''
                    },
                    'toggl_track': {
                        'email': '',
                        'password': ''
                    }
                },
                f
            )
        raise ConfigNotInitializedException(configfile.resolve())

    with configfile.open() as f:
        yml = yaml.safe_load(f)
        try:
            return Config(
                yml['jira_tempo']['user_id'],
                yml['jira_tempo']['api_token'],
                yml['toggl_track']['email'],
                yml['toggl_track']['password'],
            )
        except KeyError:
            raise KeyError(f'Could not parse config file "{configfile.resolve()}"')


def send_entries_to_tempo(date: str, entries: List[TempoEntry], config: Config):
    for entry in entries:
        data = {
            "issueKey": entry.issue_id,
            "timeSpentSeconds": entry.time_logged_seconds,
            "startDate": date,
            "startTime": "09:00:00",
            "description": entry.description,
            "authorAccountId": config.jira_tempo_user_id
        }

        response = requests.post(
            'https://api.tempo.io/core/3/worklogs/',
            json=data,
            headers={
                'Authorization': f'Bearer {config.jira_tempo_api_token}'
            }
        )
        response.raise_for_status()

        print(f'  {entry.issue_id} ✅')


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
    return p.parse_args()


def main():
    args = parse_args()
    date = args.DATE
    read_from_file = args.file

    if args.config:
        configfile = Path(args.config)
    else:
        configfile = Path.home().joinpath(CONFIG_FILE_DEFAULT_PATH)

    try:
        config = read_config_file(configfile)
    except ConfigNotInitializedException as e:
        print(f'''Config file not found: "{e}" 
The configuration file has been created now. Fill in the required options there.

For Jira Tempo Timesheets
  - Find your Jira user ID by clicking your user avatar in Jira UI and going to Profile. The ID will be in the address bar. 
  - Create an API token at "https://YOUR-WORKSPACE.atlassian.net/plugins/servlet/ac/io.tempo.jira/tempo-app#!/configuration/api-integration".'
  
For Toggl Track
  - Enter your e-mail and password to the Toggl Track service
  - This is only needed if submitting data through the Toggl Track API. Leave it empty if submitting file-based data.
''')
        exit(1)

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
            errors.append(f'Entry for {entry.issue_id} is missing a description')
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


if __name__ == '__main__':
    main()
