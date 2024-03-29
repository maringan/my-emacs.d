# coding: utf-8

try:
    unicode()
except NameError:
    unicode = str

import os
import json
import re
import hashlib
import webbrowser
from collections import defaultdict

try:
    from . import agent_connection, editor
    from .common import api, msg, shared as G, utils, reactor, ignore
    from .view import View
    from .common.handlers import base
    from .emacs_protocol import EmacsProtocol
except (ImportError, ValueError):
    import agent_connection
    import editor
    from common import api, msg, shared as G, utils, reactor, ignore
    from view import View
    from common.handlers import base
    from emacs_protocol import EmacsProtocol


try:
    import urllib
    from urllib import request
    Request = request.Request
    urlopen = request.urlopen
    HTTPError = urllib.error.HTTPError
    URLError = urllib.error.URLError
    assert Request and urlopen and HTTPError and URLError
except ImportError:
    import urllib2
    Request = urllib2.Request
    urlopen = urllib2.urlopen
    HTTPError = urllib2.HTTPError
    URLError = urllib2.URLError


def has_perm(perm):
    def outer(f):
        def inner(*args, **kwargs):
            if perm in G.PERMS:
                return f(*args, **kwargs)
        return inner
    return outer


class EmacsHandler(base.BaseHandler):
    PROTOCOL = EmacsProtocol

    def __init__(self, *args, **kwargs):
        super(EmacsHandler, self).__init__(*args, **kwargs)
        self.agent = None  # agent handler (to the backend connection)
        self.views = {}
        self.user_inputs = {}
        self.user_input_count = 0
        self.emacs_bufs = defaultdict(lambda: [""])
        self.bufs_changed = []

    def error_message(self, *args, **kwargs):
        print(args, kwargs)

    def status_message(self, *args, **kwargs):
        print(args, kwargs)

    def send_to_floobits(self, data):
        self.agent.send(data)

    def get_buf_by_path(self, path):
        if not self.agent:
            return None
        return self.agent.get_buf_by_path(path)

    def tick(self):
        reported = set()
        while self.bufs_changed:
            buf_id = self.bufs_changed.pop()
            view = self.get_view(buf_id)
            buf = view.buf
            if view.is_loading():
                msg.debug('View for buf %s is not ready. Ignoring change event' % buf['id'])
                continue
            if 'patch' not in G.PERMS:
                continue
            vb_id = view.native_id
            if vb_id in reported:
                continue
            if 'buf' not in buf:
                msg.debug('No data for buf %s %s yet. Skipping sending patch' % (buf['id'], buf['path']))
                continue

            reported.add(vb_id)
            patch = utils.FlooPatch(view.get_text(), view.buf)
            # Update the current copy of the buffer
            buf['buf'] = patch.current
            buf['md5'] = hashlib.md5(patch.current.encode('utf-8')).hexdigest()
            self.send_to_floobits(patch.to_json())

    def get_input(self, prompt, initial, cb, *args, **kwargs):
        self.user_input_count += 1
        event = {
            'name': 'user_input',
            'id': self.user_input_count,
            'prompt': prompt,
            'initial': initial,
        }
        if 'choices' in kwargs:
            event['choices'] = kwargs['choices']
        elif 'y_or_n' in kwargs:
            event['y_or_n'] = True
            del kwargs['y_or_n']
            event['prompt'] = prompt.replace('\n', ', ').replace(", ,", "") + '? '
        self.send(event)
        self.user_inputs[self.user_input_count] = lambda x: cb(x, *args, **kwargs)

    def on_connect(self):
        msg.log("have an emacs!")

    def remote_connect(self, owner, workspace, get_bufs=True):
        G.PROJECT_PATH = os.path.realpath(G.PROJECT_PATH)
        self.agent = agent_connection.AgentConnection(owner, workspace, self, get_bufs)
        reactor.reactor.connect(self.agent, G.DEFAULT_HOST, G.DEFAULT_PORT, True)
        return self.agent

    def create_view(self, buf, emacs_buf=None):
        v = View(self, buf, emacs_buf)
        self.views[buf['id']] = v
        return v

    def get_view(self, buf_id):
        """Warning: side effects!"""
        # return self.agent.get_view(buf_id)
        view = self.views.get(buf_id)
        if view:
            return view
        buf = self.agent.bufs[buf_id]
        full_path = utils.get_full_path(buf['path'])
        emacs_buf = self.emacs_bufs.get(full_path)
        if emacs_buf:
            view = self.create_view(buf, emacs_buf)
        return view

    def get_view_by_path(self, path):
        """Warning: side effects!"""
        if not path:
            return None
        buf = self.get_buf_by_path(path)
        if not buf:
            msg.debug("buf not found for path %s" % path)
            return None
        view = self.get_view(buf['id'])
        if not view:
            msg.debug("view not found for %s %s" % (buf['id'], buf['path']))
            return None
        return view

    def update_view(self, data, view):
        view.set_text(data['buf'])

    def _on_user_input(self, data):
        cb_id = int(data['id'])
        cb = self.user_inputs.get(cb_id)
        if cb is None:
            msg.error('cb for input %s is none' % cb_id)
            return
        cb(data)
        del self.user_inputs[cb_id]

    def _on_set_follow_mode(self, req):
        msg.log('follow mode is %s' % ((req.get('follow_mode') and 'enabled') or 'disabled'))

    def _on_change(self, req):
        path = req['full_path']
        view = self.get_view_by_path(path)
        changed = req['changed']
        begin = req['begin']
        old_length = req['old_length']
        self.emacs_bufs[path][0] = "%s%s%s" % (self.emacs_bufs[path][0][:begin - 1], changed, self.emacs_bufs[path][0][begin - 1 + old_length:])
        if not view:
            return
        self.bufs_changed.append(view.buf['id'])

    @has_perm('highlight')
    def _on_highlight(self, req):
        view = self.get_view_by_path(req['full_path'])
        if not view:
            return
        highlight_json = {
            'id': view.buf['id'],
            'name': 'highlight',
            'ranges': req['ranges'],
            'following': bool(req['following']),
            'ping': req.get("ping"),
        }
        msg.debug("sending highlight upstream %s" % highlight_json)
        self.send_to_floobits(highlight_json)

    @has_perm('create_buf')
    def _on_create_buf(self, req):
        # TODO: use the view state if it exists instead of uploading the on-disk state
        self.agent.upload(req['full_path'])

    @has_perm('delete_buf')
    def _on_delete_buf(self, req):
        buf = self.get_buf_by_path(req['path'])
        if not buf:
            msg.debug('No buffer for path %s' % req['path'])
            return
        msg.log('deleting buffer ', buf['path'])
        self.send_to_floobits({
            'name': 'delete_buf',
            'id': buf['id'],
        })

    @has_perm('rename_buf')
    def _on_rename_buf(self, req):
        old_path = utils.to_rel_path(req['old_path'])
        buf = self.get_buf_by_path(old_path)
        if not buf:
            msg.debug('No buffer for path %s' % req['path'])
            return
        path = utils.to_rel_path(req['path'])
        if not utils.is_shared(path):
            msg.log('New path %s is not shared. Discarding rename event.' % path)
            return
        buf_id = buf['id']
        self.send_to_floobits({
            'name': 'rename_buf',
            'id': buf['id'],
            'path': path,
        })
        # KANS: is this right? old code...
        old_path = self.agent.bufs[buf_id]['path']
        del self.agent.paths_to_ids[old_path]
        self.agent.paths_to_ids[path] = buf_id
        self.agent.bufs[buf_id]['path'] = path

    @has_perm('saved')
    def _on_saved(self, req):
        buf = self.get_buf_by_path(req['path'])
        if not buf:
            msg.debug('No buffer for path %s' % req['path'])
            return
        self.send_to_floobits({
            'name': 'saved',
            'id': buf['id'],
        })

    @has_perm('patch')
    def _on_revert(self, req):
        path = req['full_path']
        view = self.get_view_by_path(path)
        self.emacs_bufs[path][0] = req['buf']
        if not view:
            # TODO: send a create_buf?
            return
        self.bufs_changed.append(view.buf['id'])

    def _on_buffer_list_change(self, req):
        added = req.get('added') or {}
        for path, text in added.items():
            buf = self.get_buf_by_path(path)
            buf_id = buf and int(buf.get('id'))
            d = buf and self.agent.on_load.get(buf_id)
            if d:
                self.emacs_bufs[path][0] = buf['buf']
            else:
                self.emacs_bufs[path][0] = text
            if not buf:
                msg.debug('no buf for path %s' % path)
                if 'create_buf' in G.PERMS and not ignore.is_ignored(path):
                    self.agent._upload(path, text=text)
                else:
                    del self.emacs_bufs[path]
                continue
            view = self.views.get(buf_id)
            if view is None:
                self.get_view(buf_id)
            elif view.is_loading():
                view._emacs_buf = self.emacs_bufs[path]
            else:
                msg.debug('view for buf %s already exists. this is not good. we got out of sync' % buf['path'])
            if d:
                del self.agent.on_load[buf_id]
                for _, f in d.items():
                    f()

        deleted = req.get('deleted') or []
        for path in deleted:
            if self.emacs_bufs.get(path) is None:
                msg.debug('emacs deleted %s but we already deleted it from emacs_bufs' % path)
            del self.emacs_bufs[path]
            buf = self.get_buf_by_path(path)
            if buf and buf['id'] in self.views:
                del self.views[buf['id']]

        seen = set()
        current = req.get('current') or []
        for path in current:
            if self.emacs_bufs.get(path) is None:
                msg.debug('We should have buffer %s in emacs_bufs but we don\'t' % path)
            else:
                seen.add(path)

        for buf_id, view in self.views.items():
            if utils.get_full_path(view.buf['path']) not in seen:
                msg.debug('We should not have buffer %s in our views but we do.' % view.buf['path'])

    def _on_open_workspace(self, req):
        try:
            webbrowser.open(self.agent.workspace_url, new=2, autoraise=True)
        except Exception as e:
            msg.error("Couldn't open a browser: %s" % (str(e)))

    def _on_open_workspace_settings(self, req):
        try:
            webbrowser.open(self.agent.workspace_url + '/settings', new=2, autoraise=True)
        except Exception as e:
            msg.error("Couldn't open a browser: %s" % (str(e)))

    def _on_share_dir(self, data):
        file_to_share = None
        utils.reload_settings()
        G.USERNAME = data['username']
        G.SECRET = data['secret']
        dir_to_share = data['dir_to_share']
        perms = data['perms']
        editor.line_endings = data['line_endings'].find("unix") >= 0 and "\n" or "\r\n"
        dir_to_share = os.path.expanduser(dir_to_share)
        dir_to_share = utils.unfuck_path(dir_to_share)
        workspace_name = os.path.basename(dir_to_share)
        G.PROJECT_PATH = os.path.realpath(dir_to_share)
        msg.debug('%s %s %s' % (G.USERNAME, workspace_name, G.PROJECT_PATH))

        if os.path.isfile(dir_to_share):
            file_to_share = dir_to_share
            dir_to_share = os.path.dirname(dir_to_share)

        try:
            utils.mkdir(dir_to_share)
        except Exception:
            return msg.error("The directory %s doesn't exist and I can't create it." % dir_to_share)

        floo_file = os.path.join(dir_to_share, '.floo')

        info = {}
        try:
            floo_info = open(floo_file, 'rb').read().decode('utf-8')
            info = json.loads(floo_info)
        except (IOError, OSError):
            pass
        except Exception as e:
            msg.warn("Couldn't read .floo file: %s: %s" % (floo_file, str(e)))

        workspace_url = info.get('url')
        if workspace_url:
            parsed_url = api.prejoin_workspace(workspace_url, dir_to_share, {'perms': perms})
            if parsed_url:
                # TODO: make sure we create_flooignore
                # utils.add_workspace_to_persistent_json(parsed_url['owner'], parsed_url['workspace'], workspace_url, dir_to_share)
                agent = self.remote_connect(parsed_url['owner'], parsed_url['workspace'], False)
                return agent.once("room_info", lambda: agent.upload(file_to_share or dir_to_share))

        parsed_url = utils.get_workspace_by_path(dir_to_share,
                                                 lambda workspace_url: api.prejoin_workspace(workspace_url, dir_to_share, {'perms': perms}))
        if parsed_url:
            agent = self.remote_connect(parsed_url['owner'], parsed_url['workspace'], False)
            return agent.once("room_info", lambda: agent.upload(file_to_share or dir_to_share))

        def on_done(data, choices=None):
            self.get_input('Workspace name:',
                           workspace_name,
                           self._on_create_workspace,
                           workspace_name,
                           dir_to_share,
                           owner=data.get('response'),
                           perms=perms)

        try:
            r = api.get_orgs_can_admin()
        except IOError as e:
            return editor.error_message('Error getting org list: %s' % str(e))
        if r.code >= 400 or len(r.body) == 0:
            return on_done({'response': G.USERNAME})
        i = 0
        choices = []
        choices.append([G.USERNAME, i])
        for org in r.body:
            i += 1
            choices.append([org['name'], i])

        self.get_input('Create workspace owned by (%s) ' % " ".join([x[0] for x in choices]), '', on_done, choices=choices)

    def _on_create_workspace(self, data, workspace_name, dir_to_share, owner=None, perms=None):
        owner = owner or G.USERNAME
        workspace_name = data.get('response', workspace_name)

        try:
            api_args = {
                'name': workspace_name,
                'owner': owner,
            }
            if perms:
                api_args['perms'] = perms
            msg.debug(str(api_args))
            r = api.create_workspace(api_args)
        except Exception as e:
            msg.error('Unable to create workspace: %s' % unicode(e))
            return editor.error_message('Unable to create workspace: %s' % unicode(e))

        workspace_url = 'https://%s/%s/%s' % (G.DEFAULT_HOST, owner, workspace_name)

        if r.code < 400:
            msg.log('Created workspace %s' % workspace_url)
            utils.add_workspace_to_persistent_json(owner, workspace_name, workspace_url, dir_to_share)
            G.PROJECT_PATH = dir_to_share
            agent = self.remote_connect(owner, workspace_name, False)
            return agent.once("room_info", lambda: agent.upload(dir_to_share))

        msg.error('Unable to create workspace: %s' % r.body)
        if r.code not in [400, 402, 409]:
            try:
                r.body = r.body['detail']
            except Exception:
                pass
            return editor.error_message('Unable to create workspace: %s' % r.body)

        if r.code == 400:
            workspace_name = re.sub('[^A-Za-z0-9_\-\.]', '-', workspace_name)
            prompt = 'Invalid name. Workspace names must match the regex [A-Za-z0-9_\-\.]. Choose another name:'
        elif r.code == 402:
            try:
                r.body = r.body['detail']
            except Exception:
                pass
            cb = lambda data: data['response'] and webbrowser.open('https://%s/%s/settings#billing' % (G.DEFAULT_HOST, owner))
            self.get_input('%s Open billing settings?' % r.body, '', cb, y_or_n=True)
            return
        else:
            prompt = 'Workspace %s/%s already exists. Choose another name:' % (owner, workspace_name)

        return self.get_input(prompt, workspace_name, self._on_create_workspace, workspace_name, dir_to_share, owner, perms)

    def join_workspace(self, data, owner, workspace, dir_to_make=None):
        d = data['response']
        if dir_to_make:
            if d:
                d = dir_to_make
                utils.mkdir(d)
            else:
                d = ''
        if d == '':
            return self.get_input('Save workspace files to: ', G.PROJECT_PATH, self.join_workspace, owner, workspace)
        d = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(d):
            if dir_to_make:
                return msg.error("Couldn't create directory %s" % dir_to_make)
            prompt = '%s is not a directory. Create it? ' % d
            return self.get_input(prompt, '', self.join_workspace, owner, workspace, dir_to_make=d, y_or_n=True)
        try:
            G.PROJECT_PATH = d
            utils.mkdir(os.path.dirname(G.PROJECT_PATH))
            self.remote_connect(owner, workspace)
        except Exception as e:
            return msg.error("Couldn't create directory %s: %s" % (G.PROJECT_PATH, str(e)))

    def _on_join_workspace(self, data):
        workspace = data['workspace']
        owner = data['workspace_owner']
        G.USERNAME = data['username']
        G.SECRET = data['secret']
        editor.line_endings = data['line_endings'].find("unix") >= 0 and "\n" or "\r\n"
        utils.reload_settings()
        try:
            G.PROJECT_PATH = utils.get_persistent_data()['workspaces'][owner][workspace]['path']
        except Exception:
            G.PROJECT_PATH = ''

        if G.PROJECT_PATH and os.path.isdir(G.PROJECT_PATH):
            return self.remote_connect(owner, workspace)

        G.PROJECT_PATH = '~/floobits/share/%s/%s' % (owner, workspace)
        self.get_input('Save workspace files to: ', G.PROJECT_PATH, self.join_workspace, owner, workspace)

    def _on_setting(self, data):
        setattr(G, data['name'], data['value'])
        if data['name'] == 'debug':
            utils.update_log_level()
