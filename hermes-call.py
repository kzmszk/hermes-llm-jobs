"""Pinned local Hermes adapter; data in stdin, JSON result in stdout, no agent tools."""
import os, sys, json, contextlib
os.environ['HERMES_SAFE_MODE']='1'
os.environ['HERMES_IGNORE_USER_CONFIG']='1'
os.environ['HERMES_IGNORE_RULES']='1'
sys.path.insert(0, os.path.expanduser('~/.hermes/hermes-agent'))
# Fail closed if a future Hermes update makes tool selection behave differently.
with contextlib.redirect_stdout(sys.stderr):
    import toolsets
    toolsets.TOOLSETS['compass-no-tools']={'description':'No tools for data-only processing','tools':[], 'includes':[]}
    from model_tools import get_tool_definitions
    if get_tool_definitions(enabled_toolsets=['compass-no-tools'], quiet_mode=True):
        raise RuntimeError('tools_must_be_disabled')
    from hermes_cli.oneshot import _run_agent
    text, result = _run_agent(sys.stdin.read(), model='gpt-6-luna', provider='openai-codex', toolsets=['compass-no-tools'], use_config_toolsets=False)
print(json.dumps({'text':text,'model':result.get('model') or 'gpt-6-luna','completed':result.get('completed')},ensure_ascii=False))
