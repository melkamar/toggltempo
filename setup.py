from setuptools import setup, find_packages

setup(
    name='toggltempo',
    version='2.1.1',
    url='https://github.com/melkamar/toggltempo',
    author='Martin Melka',
    author_email='martin.melka@gmail.com',
    description='Synchronise your Toggl Track time entries into Jira Tempo plugin',
    packages=find_packages(),
    install_requires=[
        'requests',
        'pyyaml'
    ],
    entry_points={
        'console_scripts': [
            'toggltempo = toggltempo:main',
        ],
    },
)
