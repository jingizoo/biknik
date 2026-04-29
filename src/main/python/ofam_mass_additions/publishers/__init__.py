"""Outbound publishers for mass-additions run artefacts.

Three sinks, all optional and non-fatal on failure:

  * :mod:`slack_publisher`     — summary message + top-N exceptions to a webhook
  * :mod:`pagerduty_publisher` — incidents on hard error / high exception rate
  * :mod:`gcs_publisher`       — uploads the local audit artefacts to GCS
"""
