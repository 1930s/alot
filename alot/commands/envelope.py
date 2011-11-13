import os
import sys
import re
import glob
import logging
import email
import tempfile
from twisted.internet import defer
import threading
from email.iterators import typed_subpart_iterator

from alot import buffers
from alot.commands import Command, registerCommand
from alot import settings
from alot import helper
from alot.message import decode_header
from alot.message import encode_header
from alot.commands import globals
from alot.helper import string_decode
from alot.crypto import GPGManager


MODE = 'envelope'


@registerCommand(MODE, 'attach', help='attach files to the mail', arguments=[
    (['path'], {'help':'file(s) to attach (accepts wildcads)'})])
class AttachCommand(Command):
    def __init__(self, path=None, **kwargs):
        Command.__init__(self, **kwargs)
        self.path = path

    def apply(self, ui):
        envelope = ui.current_buffer.envelope

        if self.path:
            files = filter(os.path.isfile,
                           glob.glob(os.path.expanduser(self.path)))
            if not files:
                ui.notify('no matches, abort')
                return
        else:
            ui.notify('no files specified, abort')

        logging.info("attaching: %s" % files)
        for path in files:
            envelope.attachments.append(helper.mimewrap(path))
        ui.current_buffer.rebuild()


@registerCommand(MODE, 'sign', help='', arguments=[
    (['hint'], {'nargs':'?', 'help':''})])
class SignCommand(Command):
    def __init__(self, hint=None, **kwargs):
        Command.__init__(self, **kwargs)
        self.hint = hint

    def apply(self, ui):
        envelope = ui.current_buffer.envelope
        envelope.sign = True


@registerCommand(MODE, 'refine', help='prompt to change the value of a header',
                 arguments=[
    (['key'], {'help':'header to refine'})])
class RefineCommand(Command):
    def __init__(self, key='', **kwargs):
        Command.__init__(self, **kwargs)
        self.key = key

    def apply(self, ui):
        envelope = ui.current_buffer.envelope
        value = envelope.get(self.key, decode=True, fallback='')
        ui.commandprompt('set %s %s' % (self.key, value))


@registerCommand(MODE, 'send', help='sends mail')
class SendCommand(Command):
    @defer.inlineCallbacks
    def apply(self, ui):
        try:
            currentbuffer = ui.current_buffer  # needed to close later
            envelope = currentbuffer.envelope
            frm = envelope.get('From', decode=True)
            sname, saddr = email.Utils.parseaddr(frm)
            omit_signature = False

            # determine account to use for sending
            account = ui.accountman.get_account_by_address(saddr)
            if account == None:
                if not ui.accountman.get_accounts():
                    ui.notify('no accounts set', priority='error')
                    return
                else:
                    account = ui.accountman.get_accounts()[0]
                    omit_signature = True

            # attach signature file if present
            if account.signature and not omit_signature:
                sig = os.path.expanduser(account.signature)
                if os.path.isfile(sig):
                    if account.signature_filename:
                        name = account.signature_filename
                    else:
                        name = None
                    envelope.attach(sig, filename=name)
                else:
                    ui.notify('could not locate signature: %s' % sig,
                              priority='error')
                    if (yield ui.choice('send without signature',
                                        select='yes', cancel='no')) == 'no':
                        return
            # send
            # sign or encrypt
            #try to determine fingerprint from From-header
            hint = account.gpg_key.encode('utf-8')
            if hint == None:
                ui.notify('could not determine fingerprint', priority='error')
                return

            if envelope.encrypt or envelope.sign:
                g = GPGManager(ui)
                imail = envelope.construct_inner_mail()
                if envelope.encrypt and envelope.sign:
                    pass
                elif envelope.encrypt:
                    pass
                elif envelope.sign:
                    mail = yield g.pwdget_wrap(g.sign_mail, *[imail, hint])
                for k, v in envelope.headers.items():
                    mail[k] = v
            else:
                mail = envelope.construct_mail()

            ui.logger.debug('MAIL:\n %s' % mail)

            def thread_code():
                os.write(write_fd, account.send_mail(mail) or 'success')

            if (yield ui.choice('send?', select='yes', cancel='no')) == 'no':
                return

            clearme = ui.notify('sending..', timeout=-1)

            def afterwards(returnvalue):
                ui.clear_notify([clearme])
                if returnvalue == 'success':  # sucessfully send mail
                    ui.buffer_close(currentbuffer)
                    ui.notify('mail send successful')
                else:
                    ui.notify('failed to send: %s' % returnvalue,
                              priority='error')

            write_fd = ui.mainloop.watch_pipe(afterwards)


            thread = threading.Thread(target=thread_code)
            thread.start()
        except Exception, e:
            ui.logger.exception(e)


@registerCommand(MODE, 'edit', help='edit currently open mail')
class EditCommand(Command):
    def __init__(self, envelope=None, **kwargs):
        self.envelope = envelope
        self.openNew = (envelope != None)
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        ebuffer = ui.current_buffer
        if not self.envelope:
            self.envelope = ui.current_buffer.envelope

        #determine editable headers
        edit_headers = set(settings.config.getstringlist('general',
                                                    'edit_headers_whitelist'))
        if '*' in edit_headers:
            edit_headers = set(self.envelope.headers.keys())
        blacklist = set(settings.config.getstringlist('general',
                                                  'edit_headers_blacklist'))
        if '*' in blacklist:
            blacklist = set(self.envelope.headers.keys())
        self.edit_headers = edit_headers - blacklist
        ui.logger.info('editable headers: %s' % blacklist)

        def openEnvelopeFromTmpfile():
            # This parses the input from the tempfile.
            # we do this ourselves here because we want to be able to
            # just type utf-8 encoded stuff into the tempfile and let alot
            # worry about encodings.

            # get input
            f = open(tf.name)
            os.unlink(tf.name)
            enc = settings.config.get('general', 'editor_writes_encoding')
            template = string_decode(f.read(), enc)
            f.close()

            # call post-edit translate hook
            translate = settings.hooks.get('post_edit_translate')
            if translate:
                template = translate(template, ui=ui, dbm=ui.dbman,
                                     aman=ui.accountman, log=ui.logger,
                                     config=settings.config)
            self.envelope.parse_template(template)
            if self.openNew:
                ui.buffer_open(buffers.EnvelopeBuffer(ui, self.envelope))
            else:
                ebuffer.envelope = self.envelope
                ebuffer.rebuild()

        # decode header
        headertext = u''
        for key in edit_headers:
            value = decode_header(self.envelope.headers.get(key, ''))
            headertext += '%s: %s\n' % (key, value)

        bodytext = self.envelope.body

        # call pre-edit translate hook
        translate = settings.hooks.get('pre_edit_translate')
        if translate:
            bodytext = translate(bodytext, ui=ui, dbm=ui.dbman,
                                 aman=ui.accountman, log=ui.logger,
                                 config=settings.config)

        #write stuff to tempfile
        tf = tempfile.NamedTemporaryFile(delete=False)
        content = bodytext
        if headertext:
            content = '%s%s' % (headertext, content)
        tf.write(content.encode('utf-8'))
        tf.flush()
        tf.close()
        cmd = globals.EditCommand(tf.name, on_success=openEnvelopeFromTmpfile,
                          refocus=False)
        ui.apply_command(cmd)


@registerCommand(MODE, 'set', help='set header value', arguments=[
    #(['--append'], {'action': 'store_true', 'help':'keep previous value'}),
    (['key'], {'help':'header to refine'}),
    (['value'], {'nargs':'+', 'help':'value'})])
class SetCommand(Command):
    def __init__(self, key, value, append=False, **kwargs):
        self.key = key
        self.value = encode_header(key, ' '.join(value))
        self.append = append
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        ui.current_buffer.envelope[self.key] = self.value
        ui.current_buffer.rebuild()


@registerCommand(MODE, 'unset', help='remove header field', arguments=[
    (['key'], {'help':'header to refine'})])
class UnsetCommand(Command):
    def __init__(self, key, **kwargs):
        self.key = key
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        del(ui.current_buffer.envelope[self.key])
        ui.current_buffer.rebuild()


@registerCommand(MODE, 'toggleheaders',
                help='toggle display of all headers')
class ToggleHeaderCommand(Command):
    def apply(self, ui):
        ui.current_buffer.toggle_all_headers()
