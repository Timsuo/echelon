"""Tests may use in-process mocks or loopback test servers, never external sockets."""
import ipaddress
import socket


def loopback(host):
    if host in {None, '', 'localhost'}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install(monkeypatch=None):
    def patch(target, name, value):
        if monkeypatch is None:
            setattr(target, name, value)
        else:
            monkeypatch.setattr(target, name, value)

    original_dns = socket.getaddrinfo
    def dns(host, *args, **kwargs):
        if not loopback(host):
            raise AssertionError('External DNS disabled in tests')
        return original_dns(host, *args, **kwargs)
    patch(socket, 'getaddrinfo', dns)
    for name in ('connect', 'connect_ex', 'sendto'):
        original = getattr(socket.socket, name)
        def guarded(self, *args, _original=original, _name=name, **kwargs):
            address = args[-1] if _name == 'sendto' else args[0]
            if isinstance(address, tuple) and not loopback(address[0]):
                raise AssertionError('External network disabled in tests')
            return _original(self, *args, **kwargs)
        patch(socket.socket, name, guarded)
