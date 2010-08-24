#
# Copyright (C) 2006 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import gobject
import gtk.glade
import virtinst
import logging
import dbus
import socket

from virtManager.error import vmmErrorDialog

HV_XEN = 0
HV_QEMU = 1

CONN_SSH = 0
CONN_TCP = 1
CONN_TLS = 2

class vmmConnect(gobject.GObject):
    __gsignals__ = {
        "completed": (gobject.SIGNAL_RUN_FIRST,
                      gobject.TYPE_NONE, (str,object,object)),
        "cancelled": (gobject.SIGNAL_RUN_FIRST,
                      gobject.TYPE_NONE, ())
        }

    def __init__(self, config, engine):
        self.__gobject_init__()
        self.window = gtk.glade.XML(
                        config.get_glade_dir() + "/vmm-open-connection.glade",
                        "vmm-open-connection", domain="virt-manager")
        self.err = vmmErrorDialog(self.window.get_widget("vmm-open-connection"),
                                  0, gtk.MESSAGE_ERROR, gtk.BUTTONS_CLOSE,
                                  _("Unexpected Error"),
                                  _("An unexpected error occurred"))
        self.engine = engine
        self.window.get_widget("vmm-open-connection").hide()

        self.window.signal_autoconnect({
            "on_hypervisor_changed": self.hypervisor_changed,
            "on_connection_changed": self.connection_changed,
            "on_hostname_combo_changed": self.hostname_combo_changed,
            "on_connect_remote_toggled": self.connect_remote_toggled,

            "on_cancel_clicked": self.cancel,
            "on_connect_clicked": self.open_connection,
            "on_vmm_open_connection_delete_event": self.cancel,
            })

        self.browser = None
        self.can_browse = False

        # Set this if we can't resolve 'hostname.local': means avahi
        # prob isn't configured correctly, and we should strip .local
        self.can_resolve_local = None

        # Plain hostname resolve failed, means we should just use IP addr
        self.can_resolve_hostname = None

        self.set_initial_state()

        self.bus = None
        self.server = None
        self.can_browse = False
        try:
            self.bus = dbus.SystemBus()
            self.server = dbus.Interface(
                            self.bus.get_object("org.freedesktop.Avahi", "/"),
                            "org.freedesktop.Avahi.Server")
            self.can_browse = True
        except Exception, e:
            logging.debug("Couldn't contact avahi: %s" % str(e))

        self.reset_state()

    def cancel(self,ignore1=None,ignore2=None):
        self.close()
        self.emit("cancelled")
        return 1

    def close(self):
        self.window.get_widget("vmm-open-connection").hide()
        self.stop_browse()

    def show(self):
        win = self.window.get_widget("vmm-open-connection")
        win.present()
        self.reset_state()

    def set_initial_state(self):
        stock_img = gtk.image_new_from_stock(gtk.STOCK_CONNECT,
                                             gtk.ICON_SIZE_BUTTON)
        self.window.get_widget("connect").set_image(stock_img)
        self.window.get_widget("connect").grab_default()

        # Hostname combo box entry
        hostListModel = gtk.ListStore(str, str, str)
        host = self.window.get_widget("hostname")
        host.set_model(hostListModel)
        host.set_text_column(2)
        hostListModel.set_sort_column_id(2, gtk.SORT_ASCENDING)
        self.window.get_widget("hostname").child.connect("changed",
                                                         self.hostname_changed)

    def reset_state(self):
        self.set_default_hypervisor()
        self.window.get_widget("connection").set_active(0)
        self.window.get_widget("autoconnect").set_sensitive(True)
        self.window.get_widget("autoconnect").set_active(True)
        self.window.get_widget("hostname").get_model().clear()
        self.window.get_widget("hostname").child.set_text("")
        self.window.get_widget("connect-remote").set_active(False)
        self.stop_browse()
        self.connect_remote_toggled(self.window.get_widget("connect-remote"))
        self.populate_uri()

    def is_remote(self):
        # Whether user is requesting a remote connection
        return self.window.get_widget("connect-remote").get_active()

    def set_default_hypervisor(self):
        default = virtinst.util.default_connection()
        if default is None:
            self.window.get_widget("hypervisor").set_active(-1)
        elif default.startswith("xen"):
            self.window.get_widget("hypervisor").set_active(0)
        elif default.startswith("qemu"):
            self.window.get_widget("hypervisor").set_active(1)

    def add_service(self, interface, protocol, name, type, domain, flags):
        try:
            # Async service resolving
            res = self.server.ServiceResolverNew(interface, protocol, name,
                                                 type, domain, -1, 0)
            resint = dbus.Interface(self.bus.get_object("org.freedesktop.Avahi",
                                                        res),
                                    "org.freedesktop.Avahi.ServiceResolver")
            resint.connect_to_signal("Found", self.add_conn_to_list)
            # Synchronous service resolving
            #self.server.ResolveService(interface, protocol, name, type,
            #                           domain, -1, 0)
        except Exception, e:
            logging.exception(e)

    def remove_service(self, interface, protocol, name, type, domain, flags):
        try:
            model = self.window.get_widget("hostname").get_model()
            name = str(name)
            for row in model:
                if row[0] == name:
                    model.remove(row.iter)
        except Exception, e:
            logging.exception(e)

    def add_conn_to_list(self, interface, protocol, name, type, domain,
                         host, aprotocol, address, port, text, flags):
        try:
            model = self.window.get_widget("hostname").get_model()
            for row in model:
                if row[2] == str(name):
                    # Already present in list
                    return

            host = self.sanitize_hostname(str(host))
            model.append([str(address), str(host), str(name)])
        except Exception, e:
            logging.exception(e)

    def start_browse(self):
        if self.browser or not self.can_browse:
            return
        # Call method to create new browser, and get back an object path for it.
        interface = -1              # physical interface to use? -1 is unspec
        protocol  = 0               # 0 = IPv4, 1 = IPv6, -1 = Unspecified
        service   = '_libvirt._tcp' # Service name to poll for
        flags     = 0               # Extra option flags
        domain    = ""              # Domain to browse in. NULL uses default
        bpath = self.server.ServiceBrowserNew(interface, protocol, service,
                                              domain, flags)

        # Create browser interface for the new object
        self.browser = dbus.Interface(self.bus.get_object("org.freedesktop.Avahi",
                                                          bpath),
                                      "org.freedesktop.Avahi.ServiceBrowser")

        self.browser.connect_to_signal("ItemNew", self.add_service)
        self.browser.connect_to_signal("ItemRemove", self.remove_service)

    def stop_browse(self):
        if self.browser:
            del(self.browser)
            self.browser = None

    def hostname_combo_changed(self, src):
        model = src.get_model()
        txt = src.child.get_text()
        row = None

        for currow in model:
            if currow[2] == txt:
                row = currow
                break

        if not row:
            return

        ip = row[0]
        host = row[1]
        entry = host
        if not entry:
            entry = ip

        self.window.get_widget("hostname").child.set_text(entry)

    def hostname_changed(self, src):
        self.populate_uri()

    def hypervisor_changed(self, src):
        self.populate_uri()

    def connect_remote_toggled(self, src):
        is_remote = self.is_remote()
        self.window.get_widget("hostname").set_sensitive(is_remote)
        self.window.get_widget("connection").set_sensitive(is_remote)
        self.window.get_widget("autoconnect").set_active(not is_remote)
        if is_remote and self.can_browse:
            self.start_browse()
        else:
            self.stop_browse()

        self.populate_uri()

    def connection_changed(self, src):
        self.populate_uri()

    def populate_uri(self):
        uri = self.generate_uri()
        self.window.get_widget("uri-entry").set_text(uri)

    def generate_uri(self):
        hv = self.window.get_widget("hypervisor").get_active()
        conn = self.window.get_widget("connection").get_active()
        host = self.window.get_widget("hostname").child.get_text()
        is_remote = self.is_remote()

        user = "root"
        if conn == CONN_SSH and '@' in host:
            user, host = host.split('@',1)

        hvstr = ""
        if hv == HV_XEN:
            hvstr = "xen"
        else:
            hvstr = "qemu"

        hoststr = ""
        if not is_remote:
            hoststr = ":///"
        elif conn == CONN_TLS:
            hoststr = "+tls://" + host + "/"
        elif conn == CONN_SSH:
            hoststr = "+ssh://" + user + "@" + host + "/"
        elif conn == CONN_TCP:
            hoststr = "+tcp://" + host + "/"

        uri = hvstr + hoststr
        if hv == HV_QEMU:
            uri += "system"

        return uri

    def validate(self):
        is_remote = self.is_remote()
        host = self.window.get_widget("hostname").child.get_text()

        if is_remote and not host:
            return self.err.val_err(_("A hostname is required for "
                                      "remote connections."))

        return True

    def open_connection(self, ignore):
        if not self.validate():
            return

        readonly = False
        auto = False
        if self.window.get_widget("autoconnect").get_property("sensitive"):
            auto = self.window.get_widget("autoconnect").get_active()
        uri = self.generate_uri()

        logging.debug("Generate URI=%s, auto=%s, readonly=%s" %
                      (uri, auto, readonly))
        self.close()
        self.emit("completed", uri, readonly, auto)

    def sanitize_hostname(self, host):
        if host == "linux" or host == "localhost":
            host = ""
        if host.startswith("linux-"):
            tmphost = host[6:]
            try:
                long(tmphost)
                host = ""
            except ValueError:
                pass

        if host:
            host = self.check_resolve_host(host)
        return host

    def check_resolve_host(self, host):
        # Try to resolve hostname
        # XXX: Avahi always uses 'hostname.local', but for some reason
        #      fedora out of the box can't resolve '.local' names
        #      Attempt to resolve the name. If it fails, remove .local
        #      if present, and try again
        if host.endswith(".local"):
            if self.can_resolve_local == False:
                host = host[:-6]

            elif self.can_resolve_local == None:
                try:
                    socket.getaddrinfo(host, None)
                except:
                    logging.debug("Couldn't resolve host '%s'. Stripping "
                                  "'.local' and retrying." % host)
                    self.can_resolve_local = False
                    host = self.check_resolve_host(host[:-6])
                else:
                    self.can_resolve_local = True

        else:
            if self.can_resolve_hostname == False:
                host = ""
            elif self.can_resolve_hostname == None:
                try:
                    socket.getaddrinfo(host, None)
                except:
                    logging.debug("Couldn't resolve host '%s'. Disabling "
                                  "host name resolution, only using IP addr" %
                                  host)
                    self.can_resolve_hostname = False
                else:
                    self.can_resolve_hostname = True

        return host


gobject.type_register(vmmConnect)
