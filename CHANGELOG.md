# Changelog

## 2.1.3
- Fix CEST/CET timezone issue. The timezone is now determined using the `tzlocal` lib.

## 2.1.2

- Fetch worklogs from Toggl Tempo using a tz-aware datetimes. The timezone is determined from the system locale.

## 2.1.1

- Switch to using Tempo API v4. v3 is deprecated and will return HTTP 410 gone when trying to use it.

## 2.0.0

- Add the `--import JRA-1234` option to import a Jira ticket as a Toggl Tempo project. Note that this is backwards
  incompatible due to the changes in the configuration file. You will be told what to do.

## 1.0.1

- Skip `#nobill` tags from being reported to Tempo
