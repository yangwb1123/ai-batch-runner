"""Local observation-only binding: no commands, provider sessions or file scans."""
import json

from .hub_client import validate_hub_url


def bind_observation(store, options, client):
    """A state directory cannot silently switch project, Hub, or execution mode."""
    previous = store.metadata('observation_binding')
    if not options.observe_only:
        if previous:
            raise ValueError('observation state cannot be reused for an executing Gateway')
        return
    origin = validate_hub_url(options.hub_url)
    if getattr(client, 'base_url', None) != origin:
        raise ValueError('observation client does not match the configured Hub')
    binding = json.dumps({'hub': origin, 'workspace': options.cwd,
                          'project': options.project_id, 'instance': options.instance_id},
                         sort_keys=True, separators=(',', ':'))
    if previous and previous != binding:
        raise ValueError('observation state has a different fixed binding')
    if store.has_execution_state() or (not previous and store.metadata('instance_id')):
        raise ValueError('observation requires its own state, not prior execution state')
    if not previous:
        store.set_metadata('observation_binding', binding)


def require_execution(options):
    if options.observe_only:
        raise ValueError('observation Gateway cannot create sessions or execute commands')
