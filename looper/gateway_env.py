"""POSIX client setup shared by loopercli and the dashboard's gateway example."""

import shlex


def gateway_environment(url: str, alias: str, key: str = '<local-key>') -> str:
    env = {
        'ANTHROPIC_BASE_URL': url,
        'ANTHROPIC_AUTH_TOKEN': key,
        'ANTHROPIC_API_KEY': '',
        **dict.fromkeys(('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                         'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL',
                         'CLAUDE_CODE_SUBAGENT_MODEL'), alias),
        'CLAUDE_CODE_ATTRIBUTION_HEADER': '0',
        'ENABLE_TOOL_SEARCH': 'false',
        'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS': '1',
    }
    return '\n'.join(f'export {name}={shlex.quote(value)}' for name, value in env.items())
