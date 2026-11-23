"""Online definitions from the assistant-layer RFC, read from manager_metrics.get.

These observations are separate from conversation scores. Missing samples stay null;
fake observations validate plumbing and must never become an online baseline.
"""
from copy import deepcopy
from typing import Any


ONLINE_DEFINITIONS = {
    'receipt_latency': {'field': 'ack', 'definition':
        'P50/P95 ack_ms, overall and by chat; user turns only, excluding replayed turns and null samples.'},
    'first_reply_latency': {'field': 'first_reply', 'definition':
        'P50/P95 first_reply_ms, overall and by chat; same exclusions; channel handoff, not platform delivery.'},
    'empty_delivery': {'field': 'empty', 'definition':
        'Empty deliveries: count and ratio among measured non-replayed user model turns.'},
    'proactivity': {'field': 'proactive', 'definition':
        'Daily messages by source in the server timezone; reply rate by source and overall among successfully '
        'sent messages at least 30 minutes old, with a reply within 30 minutes.'},
}


def online_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    """Preserve the API's values and sample absence, without recomputing percentiles."""
    result = {'source': 'manager_metrics.get', 'window_days': summary.get('window_days'),
              'enabled': summary['enabled'], 'definitions': ONLINE_DEFINITIONS,
              'turns': deepcopy(summary.get('turns')), 'values': None}
    if summary['enabled']:
        missing = [spec['field'] for spec in ONLINE_DEFINITIONS.values() if spec['field'] not in summary]
        if missing:
            raise ValueError('manager_metrics missing fields: ' + ', '.join(missing))
        result['values'] = {name: deepcopy(summary[spec['field']]) for name, spec in ONLINE_DEFINITIONS.items()}
    return result
