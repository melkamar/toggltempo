# 2.1.0

- Switch to using Tempo API v4. v3 is deprecated and will return HTTP 410 gone when trying to use it.

# 2.0.0

- Add the `--import JRA-1234` option to import a Jira ticket as a Toggl Tempo project. Note that this is backwards
  incompatible due to the changes in the configuration file. You will be told what to do.

# 1.0.1

- Skip `#nobill` tags from being reported to Tempo
