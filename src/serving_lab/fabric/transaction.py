"""Reservation identity and phase token helpers for the V7 wire contract."""
import hashlib


def reservation_id(request_id, generation):
    return hashlib.sha256(f'{request_id}|{generation}|reservation/v7'.encode()).hexdigest()[:24]


def phase_token(reservation, phase, epoch):
    return hashlib.sha256(f'{reservation}|{phase}|{epoch}'.encode()).hexdigest()[:24]


def feedback_signature(trace_id, phase_id, sample, resource, epoch, service_us):
    material = f'{trace_id}|{phase_id}|{sample}|{resource}|{epoch}|{service_us}'
    return hashlib.sha256(material.encode()).hexdigest()


def ack_matches(ack, request_id, resource, layout, token, phase, transaction,
                route_id=None, route_generation=None, topology_epoch=None):
    if not (isinstance(ack, dict) and ack.get('request_id') == request_id and
            ack.get('resource') == resource and ack.get('layout') == layout and
            ack.get('transaction_id') == transaction and
            ack.get('phase') == phase and ack.get('phase_token') == token and
            isinstance(ack.get('kv_handle'), str) and bool(ack['kv_handle'])):
        return False
    if route_id is not None and ack.get('route_id') != route_id:
        return False
    if route_generation is not None:
        if type(ack.get('route_generation')) is not int or ack['route_generation'] != route_generation:
            return False
    if topology_epoch is not None:
        if type(ack.get('topology_epoch')) is not int or ack['topology_epoch'] != topology_epoch:
            return False
    return True


def decode_receipt(headers, row, resource, layout):
    expected = {'x-ack-request-id': row['request_id'], 'x-ack-transaction-id': row['transaction_id'],
                'x-ack-phase': 'decode', 'x-ack-phase-token': row['phase_tokens']['decode'],
                'x-ack-route-id': row['route_id'], 'x-ack-route-generation': str(row['route_generation']),
                'x-ack-topology-epoch': str(row['topology_epoch']), 'x-ack-resource': resource,
                'x-ack-layout': layout}
    return all(headers.get(key) == value for key, value in expected.items())
