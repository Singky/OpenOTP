from otp import config

import asyncio

from dc.util import Datagram
from otp.networking import ChannelAllocator, ToontownProtocol, DatagramFuture
from otp.messagedirector import DownstreamMessageDirector, MDParticipant, MDUpstreamProtocol, UpstreamServer

import time


from enum import IntEnum


from dc.parser import parse_dc_file
from dc.objects import MolecularField

from otp.zone import *

from .constants import *

import json

from dataclasses import dataclass
from dataslots import with_slots


from otp.messagetypes import *

from Crypto.Cipher import AES


@with_slots
@dataclass
class PotentialAvatar:
    do_id: int
    name: str
    wish_name: str
    approved_name: str
    rejected_name: str
    dna_string: str
    index: int


class ClientState(IntEnum):
    NEW = 0
    ANONYMOUS = 1
    AUTHENTICATED = 2


class ClientDisconnect(IntEnum):
    INTERNAL_ERROR = 1
    RELOGGED = 100
    CHAT_ERROR = 120
    LOGIN_ERROR = 122
    OUTDATED_CLIENT = 127
    ADMIN_KICK = 151
    ACCOUNT_SUSPENDED = 152
    SHARD_DISCONNECT = 153
    PERIOD_EXPIRED = 288
    PERIOD_EXPIRED2 = 349


class Interest:
    def __init__(self, client, handle, context, parent_id, zones):
        self.client = client
        self.handle = handle
        self.context = context
        self.parent_id = parent_id
        self.zones = zones
        self.done = False


OTP_DO_ID_TOONTOWN = 4618


@with_slots
@dataclass
class ObjectInfo:
    do_id: int
    dc_id: int
    parent_id: int
    zone_id: int


CLIENTAGENT_SECRET = bytes.fromhex(config['General.LOGIN_SECRET'])


class ClientProtocol(ToontownProtocol, MDParticipant):
    def __init__(self, service):
        ToontownProtocol.__init__(self, service)
        MDParticipant.__init__(self, service)

        self.state = ClientState.NEW
        self.channel = service.new_channel_id()
        self.subscribe_channel(self.channel)
        self.alloc_channel = self.channel

        self.session_objects = set()

        self.interests = []
        self.visible_objects = {}
        self.pending_objects = {}
        self.owned_objects = {}
        self.callbacks = {}

        self.account_data = None

        self.current_context = None

        self.pending_set_av = None

    def disconnect(self, booted_index, booted_text):
        for task in self.tasks:
            task.cancel()
        del self.tasks[:]
        resp = Datagram()
        resp.add_uint16(CLIENT_GO_GET_LOST)
        resp.add_uint16(booted_index)
        resp.add_string16(booted_text.encode('utf-8'))
        self.transport.write(resp.get_length().to_bytes(2, byteorder='little'))
        self.transport.write(resp.get_message().tobytes())
        self.transport.close()

    def connection_lost(self, exc):
        ToontownProtocol.connection_lost(self, exc)

        if self.pending_set_av:
            dg = Datagram()
            dg.add_server_header([STATESERVERS_CHANNEL], self.channel, STATESERVER_OBJECT_DELETE_RAM)
            dg.add_uint32(self.pending_set_av)
            self.service.send_datagram(dg)

        self.service.remove_participant(self)
        print('lost connection to client!', exc)

    def data_received(self, data: bytes):
        ToontownProtocol.data_received(self, data)

    def receive_datagram(self, dg):
        print('got dg...', dg.get_message().tobytes())
        dgi = dg.iterator()
        msgtype = dgi.get_uint16()

        print('GOT MSGTYPE %s' % msgtype)

        if msgtype == CLIENT_HEARTBEAT:
            self.send_datagram(dg)
            return

        if self.state == ClientState.NEW:
            if msgtype == CLIENT_LOGIN_TOONTOWN:
                self.receive_login(dgi)
                self.state = ClientState.AUTHENTICATED
        elif self.state == ClientState.AUTHENTICATED:
            if msgtype == CLIENT_ADD_INTEREST:
                self.receive_add_interest(dgi)
            elif msgtype == CLIENT_REMOVE_INTEREST:
                self.receive_remove_interest(dgi)
            elif msgtype == CLIENT_GET_AVATARS:
                self.receive_get_avatars(dgi)
            elif msgtype == CLIENT_SET_WISHNAME:
                self.receive_set_wishname(dgi)
            elif msgtype == CLIENT_CREATE_AVATAR:
                self.receive_create_avatar(dgi)
            elif msgtype == CLIENT_SET_AVATAR:
                self.receive_set_avatar(dgi)
            elif msgtype == CLIENT_GET_FRIEND_LIST:
                self.receive_get_friend_list(dgi)
            elif msgtype == CLIENT_OBJECT_LOCATION:
                self.receive_client_location(dgi)
            elif msgtype == CLIENT_DISCONNECT:
                pass
            elif msgtype == CLIENT_SET_SECURITY:
                # string token
                # int32 tokenType
                pass
            elif msgtype == CLIENT_OBJECT_UPDATE_FIELD:
                self.receive_update_field(dgi)

    def receive_update_field(self, dgi):
        do_id = dgi.get_uint32()
        field_number = dgi.get_uint16()

        field = self.service.dc_file.fields[field_number]()

        pos = dgi.tell()
        try:
            field.unpack_bytes(dgi)
        except Exception as e:
            print('couldnt unpack field', e)
        dgi.seek(pos)

        print('receive_update_field', do_id, field.name)

        if do_id in self.owned_objects:
            resp = Datagram()
            resp.add_server_header([do_id], self.channel, STATESERVER_OBJECT_UPDATE_FIELD)
            resp.add_uint32(do_id)
            resp.add_uint16(field_number)
            resp.add_bytes(dgi.get_remaining())
            self.service.send_datagram(resp)
            return

    def receive_client_location(self, dgi):
        do_id = dgi.get_uint32()
        parent_id = dgi.get_uint32()
        zone_id = dgi.get_uint32()

        print('client_location', do_id, parent_id, zone_id)

        if do_id in self.owned_objects:
            print('sending set zone upstream...')
            dg = Datagram()
            dg.add_server_header([do_id], self.channel, STATESERVER_OBJECT_SET_ZONE)
            dg.add_uint32(parent_id)
            dg.add_uint32(zone_id)
            self.service.send_datagram(dg)

            #resp = Datagram()
            #resp.add_uint16(CLIENT_OBJECT_LOCATION)
            #resp.add_uint32(parent_id)
            #resp.add_uint32(zone_id)
            #self.send_datagram(resp)

    def receive_get_friend_list(self, dgi):
        error = 0

        count = 0

        # Friend Structure
        # uint32 do_id
        # string name
        # string dna_string
        # uint32 pet_id

        resp = Datagram()
        resp.add_uint16(CLIENT_GET_FRIEND_LIST_RESP)
        resp.add_uint8(error)
        resp.add_uint16(count)

        self.send_datagram(resp)

    def receive_set_avatar(self, dgi):
        av_id = dgi.get_uint32()

        if not av_id:
            print('AV ID IS 0 FOR SET AVATAR')
            return

        print('SET AVATAR %s' % av_id)

        self.pending_set_av = av_id

        self.tasks.append(self.service.loop.create_task(self.handle_avatar_info()))

        dg = Datagram()
        dg.add_server_header([DBSERVERS_CHANNEL], self.channel, DBSERVER_GET_STORED_VALUES)
        dg.add_uint32(1)
        dg.add_uint32(av_id)

        pos = dg.tell()
        dg.add_uint16(0)
        count = 0
        for field in self.service.dc_file.namespace['DistributedToon']:
            if not isinstance(field, MolecularField) and field.is_required and 'db' in field.keywords:
                dg.add_uint16(field.number)
                count += 1

        dg.seek(pos)
        dg.add_uint16(count)
        self.service.send_datagram(dg)

    def receive_create_avatar(self, dgi):
        _ = dgi.get_uint16()
        dna = dgi.get_string16()
        pos = dgi.get_uint8()
        print('create avatar request', dna, pos)

        dclass = self.service.dc_file.namespace['DistributedToon']

        dg = Datagram()
        dg.add_server_header([DBSERVERS_CHANNEL], self.channel, DBSERVER_CREATE_STORED_OBJECT)
        dg.add_uint32(0)
        dg.add_uint16(dclass.number)
        dg.add_uint32(self.account_data['disl_id'])
        dg.add_uint8(pos)
        pos = dg.tell()
        dg.add_uint16(0)

        print('packing fields...')

        from ai.DistributedToon import DistributedToonAI
        obj = DistributedToonAI(self.service)
        obj.getDISLid = lambda self: self.account_data['disl_id']

        try:
            count = 0
            for field in dclass.inherited_fields:
                if field.number == dclass['setDNAString'].number:
                    dg.add_uint16(field.number)
                    dg.add_string16(dna.encode('ascii'))
                    count += 1
                elif not isinstance(field, MolecularField) and field.is_required and 'db' in field.keywords:
                    print('packing %s...' % field.name)
                    dg.add_uint16(field.number)
                    dclass.pack_field(dg, obj, field)
                    count += 1

            dg.seek(pos)
            dg.add_uint16(count)

            self.service.send_datagram(dg)

        except Exception as e:
            print(e, e.__class__, e.args)

        self.tasks.append(self.service.loop.create_task(self.created_avatar()))

    async def created_avatar(self):
        f = DatagramFuture(self.service.loop, DBSERVER_CREATE_STORED_OBJECT_RESP)
        self.futures.append(f)
        sender, dgi = await f
        return_code = dgi.get_uint8()
        av_id = dgi.get_uint32()

        resp = Datagram()
        resp.add_uint16(CLIENT_CREATE_AVATAR_RESP)
        resp.add_uint16(0)  # Context
        resp.add_uint8(return_code)  # Return Code
        resp.add_uint32(av_id)  # av_id

        print('made av', av_id, return_code)

        self.send_datagram(resp)

    def receive_set_wishname(self, dgi):
        av_id = dgi.get_uint32()
        name = dgi.get_string16()

        print('set_wishname', av_id, name)

        pending = name.encode('utf-8')
        approved = b''
        rejected = b''

        failed = False

        resp = Datagram()
        resp.add_uint16(CLIENT_SET_WISHNAME_RESP)
        resp.add_uint32(av_id)
        resp.add_uint16(failed)
        resp.add_string16(pending)
        resp.add_string16(approved)
        resp.add_string16(rejected)

        self.send_datagram(resp)
        print('sent wishname resp')

    def receive_remove_interest(self, dgi):
        handle = dgi.get_uint16()

        if dgi.remaining():
            context = dgi.get_uint32()
        else:
            context = None

        interest = None

        for _interest in self.interests:
            if _interest.handle == handle and _interest.context == context:
                interest = _interest
                break

        if not interest:
            print('Got Remove interest for unknown interest:', handle, context)
            return

        parent_id = interest.parent_id

        uninterested_zones = []

        for zone in interest.zones:
            if len(self.lookup_interest(parent_id, zone)) == 1:
                uninterested_zones.append(zone)

        to_remove = []

        for do_id in self.visible_objects:
            do = self.visible_objects[do_id]
            if do.parent_id == parent_id and do.zone_id in uninterested_zones:
                self.send_remove_object(do_id)

                to_remove.append(do_id)

        for do_id in to_remove:
            del self.visible_objects[do_id]

        for zone in uninterested_zones:
            print('unSUBSCRIBING FROM ', parent_id, zone, location_as_channel(parent_id, zone))
            self.unsubscribe_channel(location_as_channel(parent_id, zone))

    def receive_get_avatars(self, dgi):
        print('querying for avatars...')
        query = Datagram()
        query.add_server_header([DBSERVERS_CHANNEL, ], self.channel, DBSERVER_ACCOUNT_QUERY)

        disl_id = self.account_data['disl_id']
        query.add_uint32(disl_id)
        field_number = self.service.avatars_field.number
        print('field_number', field_number)
        query.add_uint16(field_number)
        self.service.send_datagram(query)

        self.tasks.append(self.service.loop.create_task(self.do_login()))

    async def do_login(self):
        f = DatagramFuture(self.service.loop, DBSERVER_ACCOUNT_QUERY_RESP)
        self.futures.append(f)
        sender, dgi = await f

        pos = dgi.tell()

        avatar_info = []

        for i in range(dgi.get_uint16()):
            pot_av = PotentialAvatar(do_id=dgi.get_uint32(), name=dgi.get_string16(), wish_name=dgi.get_string16(),
                                     approved_name=dgi.get_string16(), rejected_name=dgi.get_string16(),
                                     dna_string=dgi.get_string16(), index=dgi.get_uint8())

            avatar_info.append(pot_av)

            dgi.get_uint8()

        self.avatar_info = avatar_info

        resp = Datagram()
        resp.add_uint16(CLIENT_GET_AVATARS_RESP)
        dgi.seek(pos)
        resp.add_uint8(0)  # Return code
        resp.add_bytes(dgi.get_remaining())
        self.send_datagram(resp)

    def receive_login(self, dgi):
        play_token = dgi.get_string16()
        server_version = dgi.get_string16()
        hash_val = dgi.get_uint32()
        want_magic_words = dgi.get_string16()

        self.service.log.debug(f'play_token:{play_token}, server_version:{server_version}, hash_val:{hash_val}, want_magic_words:{want_magic_words}')

        try:
            play_token = bytes.fromhex(play_token)
            nonce, tag, play_token = play_token[:16], play_token[16:32], play_token[32:]
            cipher = AES.new(CLIENTAGENT_SECRET, AES.MODE_EAX, nonce)
            data = cipher.decrypt_and_verify(play_token, tag)
        except ValueError as e:
            self.disconnect(ClientDisconnect.LOGIN_ERROR, 'Invalid token')
            return


        print('loading json....')

        data = json.loads(data)

        for key in list(data.keys()):
            if type(data[key]) == str:
                data[key] = data[key].encode('utf-8')

        print('data:', data)

        resp = Datagram()
        resp.add_uint16(CLIENT_LOGIN_TOONTOWN_RESP)

        return_code = 0  # -13 == period expired
        resp.add_uint8(return_code)

        error_string = b'' # 'Bad DC Version Compare'
        resp.add_string16(error_string)

        resp.add_uint32(data['disl_id'])
        resp.add_string16(data['username'])
        account_name_approved = True
        resp.add_uint8(account_name_approved)
        resp.add_string16(data['whitelist_chat_enabled'])
        resp.add_string16(data['create_friends_with_chat'])
        resp.add_string16(data['chat_code_creation_rule'])

        print('added strings')

        now = round(time.time(), 6)
        seconds = int(now)
        useconds = 0 #int(str(now).split('.')[1])
        print(seconds, useconds)

        resp.add_uint32(seconds)
        resp.add_uint32(useconds)

        resp.add_string16(data['access'])
        resp.add_string16(data['whitelist_chat_enabled'])

        last_logged_in = time.strftime('%c')  # time.strftime('%c')
        resp.add_string16(last_logged_in.encode('utf-8'))

        account_days = 0
        resp.add_int32(account_days)
        resp.add_string16(data['account_type'])
        resp.add_string16(data['username'])

        print('sending...')
        self.send_datagram(resp)
        self.account_data = data

    def receive_add_interest(self, dgi):
        handle = dgi.get_uint16()
        context_id = dgi.get_uint32()
        parent_id = dgi.get_uint32()

        num_zones = dgi.remaining() // 4

        zones = []

        for i in range(num_zones):
            zones.append(dgi.get_uint32())

        print('CLIENT_ADD_INTEREST', handle, context_id, parent_id, zones)

        interest = Interest(self.channel, handle, context_id, parent_id, zones)
        self.interests.append(interest)

        query_request = Datagram()
        query_request.add_server_header([parent_id], self.channel, STATESERVER_QUERY_ZONE_OBJECT_ALL)
        query_request.add_uint16(handle)
        query_request.add_uint32(context_id)
        query_request.add_uint32(parent_id)

        for zone in zones:
            query_request.add_uint32(zone)
            print('SUBSCRIBING TO ', parent_id, zone, location_as_channel(parent_id, zone))
            self.subscribe_channel(location_as_channel(parent_id, zone))

        print(query_request.get_message().tobytes())
        self.service.send_datagram(query_request)

    def handle_datagram(self, dg, dgi):
        sender = dgi.get_channel()

        if sender == self.channel:
            return

        msgtype = dgi.get_uint16()

        print('handle_datagram', sender, msgtype)

        self.check_futures(dgi, msgtype, sender)

        if msgtype == STATESERVER_OBJECT_ENTERZONE_WITH_REQUIRED_OTHER:
            self.handle_object_entrance(dgi, sender)
        elif msgtype == STATESERVER_OBJECT_ENTER_OWNER_RECV:
            self.handle_owned_object_entrance(dgi, sender)
        elif msgtype == STATESERVER_OBJECT_CHANGE_ZONE:
            self.handle_location_change(dgi, sender)
        elif msgtype == STATESERVER_QUERY_ZONE_OBJECT_ALL_DONE:
            self.handle_interest_done(dgi)
        elif msgtype == STATESERVER_OBJECT_UPDATE_FIELD:
            self.handle_update_field(dgi, sender)
        #elif msgtype == DBSERVER_CREATE_STORED_OBJECT_RESP:
        #    self.created_avatar(dgi)
        #elif msgtype == DBSERVER_GET_STORED_VALUES_RESP:
        #    self.got_avatar_info(dgi)
        else:
            print('CLIENT', 'unhandled', msgtype, dg.get_message())

    async def handle_avatar_info(self):
        f = DatagramFuture(self.service.loop, DBSERVER_GET_STORED_VALUES_RESP)
        self.futures.append(f)
        sender, dgi = await f

        context = dgi.get_uint32()
        field_count = dgi.get_uint16()
        print('field_count', field_count)

        fields = {}

        #AtomicField setAccess ['broadcast', 'ownrecv', 'required', 'ram', 'airecv'] 131
        #AtomicField setAsGM ['required', 'ram', 'broadcast', 'ownrecv', 'airecv'] 132
        #AtomicField setBattleId ['required', 'broadcast', 'ram'] 421

        # AtomicField WishName ['db', 'ram'] 128
        # AtomicField WishNameState ['db', 'ram'] 129
        # AtomicField setDISLid ['ram', 'db', 'airecv'] 587

        dclass = self.service.dc_file.namespace['DistributedToon']

        o = ObjectInfo(self.pending_set_av, dclass.number, 0, 0)
        self.owned_objects[self.pending_set_av] = o

        for i in range(field_count):
            number = dgi.get_uint16()
            print('field', number)
            fields[number] = self.service.dc_file.fields[number]().unpack_bytes(dgi)

        print(fields)

        access_field = dclass['setAccess']
        prev_access_field = dclass['setPreviousAccess']
        as_gm_field = dclass['setAsGM']
        battle_id = dclass['setBattleId']

        fields[access_field.number] = fields[prev_access_field.number]
        fields[battle_id.number] = b'\x00\x00\x00\x00'
        fields[as_gm_field.number] = b'\x01'

        account_id = self.account_data['disl_id']
        sender_channel = account_id << 32 | self.pending_set_av
        self.channel = sender_channel
        self.subscribe_channel(self.channel)

        dg = Datagram()
        dg.add_server_header([STATESERVERS_CHANNEL], self.channel, STATESERVER_OBJECT_GENERATE_WITH_REQUIRED)
        dg.add_uint32(0)
        dg.add_uint32(0)
        dg.add_uint16(dclass.number)
        dg.add_uint32(self.pending_set_av)

        for field in dclass.inherited_fields:
            if field.is_required:
                print('SS pack %s' % field.name)
                dg.add_bytes(fields[field.number])

        self.service.send_datagram(dg)

        self.av_fields = fields

        dg = Datagram()
        dg.add_server_header([STATESERVERS_CHANNEL], self.channel, STATESERVER_OBJECT_SET_OWNER_RECV)
        dg.add_uint32(self.pending_set_av)
        dg.add_channel(self.channel)
        self.service.send_datagram(dg)

    def handle_update_field(self, dgi, sender):
        if sender == self.channel:
            return

        resp = Datagram()
        resp.add_uint16(CLIENT_OBJECT_UPDATE_FIELD)
        resp.add_bytes(dgi.get_remaining())
        self.send_datagram(resp)

    def handle_owned_object_entrance(self, dgi, sender):
        print('GOT OWNERRECV')
        do_id = dgi.get_uint32()
        parent_id = dgi.get_uint32()
        zone_id = dgi.get_uint32()
        dc_id = dgi.get_uint16()

        dclass = self.service.dc_file.classes[dc_id]

        resp = Datagram()
        resp.add_uint16(CLIENT_GET_AVATAR_DETAILS_RESP)
        resp.add_uint32(self.pending_set_av)
        resp.add_uint8(0)  # Return code

        for field in dclass.inherited_fields:
            if not isinstance(field, MolecularField) and field.is_required:
                print('pack %s' % field.name)
                resp.add_bytes(self.av_fields[field.number])

        self.send_datagram(resp)

    def handle_location_change(self, dgi, sender):
        do_id = dgi.get_uint32()

        # TODO: if do_id is from pending interest, queue the location change.

        new_parent = dgi.get_uint32()
        new_zone = dgi.get_uint32()
        old_parent = dgi.get_uint32()
        old_zone = dgi.get_uint32()
        print('CA', 'location_change', new_parent, new_zone, do_id)

        disable = True

        for interest in self.interests:
            if interest.parent_id == new_parent and new_zone in interest.zones:
                disable = False
                break

        visible = do_id in self.visible_objects
        owned = do_id in self.owned_objects

        if not visible and not owned:
            return

        if visible:
            self.visible_objects[do_id].parent_id = new_parent
            self.visible_objects[do_id].zone_id = new_zone

        if owned:
            self.owned_objects[do_id].parent_id = new_parent
            self.owned_objects[do_id].zone_id = new_zone

        if disable and visible:
            if owned:
                # TODO
                pass
            self.send_remove_object(do_id)
            del self.visible_objects[do_id]
        else:
            print('sending object location!')
            #self.send_object_location(do_id, new_parent, new_zone)

    def send_remove_object(self, do_id):
        resp = Datagram()
        resp.add_uint16(CLIENT_OBJECT_DISABLE)
        resp.add_uint32(do_id)
        self.send_datagram(resp)

    def send_object_location(self, do_id, new_parent, new_zone):
        resp = Datagram()
        resp.add_uint16(CLIENT_OBJECT_LOCATION)
        resp.add_uint32(do_id)
        resp.add_uint32(new_parent)
        resp.add_uint32(new_zone)

        self.send_datagram(resp)

    def handle_interest_done(self, dgi):
        handle = dgi.get_uint16()
        context = dgi.get_uint32()
        print('sending interest done', handle, context)

        interest = None

        for _interest in self.interests:
            if _interest.handle == handle and _interest.context == context:
                interest = _interest
                break

        if not interest:
            print('Got interest done for unknown interest:', handle, context)
            return

        if interest.done:
            print('Received duplicate interest done...')
            return

        interest.done = True

        resp = Datagram()
        resp.add_uint16(CLIENT_DONE_INTEREST_RESP)
        resp.add_uint16(handle)
        resp.add_uint32(context)
        self.send_datagram(resp)

    def lookup_interest(self, parent_id, zone_id):
        return [interest for interest in self.interests if interest.parent_id == parent_id and zone_id in interest.zones]

    def handle_object_entrance(self, dgi, sender):
        has_other = dgi.get_uint8()
        do_id = dgi.get_uint32()

        if do_id in self.owned_objects:
            return

        parent_id = dgi.get_uint32()
        zone_id = dgi.get_uint32()
        dc_id = dgi.get_uint16()

        self.visible_objects[do_id] = ObjectInfo(do_id, dc_id, parent_id, zone_id)

        self.send_object_entrance(parent_id, zone_id, dc_id, do_id, dgi, has_other)

    def send_object_entrance(self, parent_id, zone_id, dc_id, do_id, dgi, has_other):
        resp = Datagram()
        resp.add_uint16(CLIENT_CREATE_OBJECT_REQUIRED_OTHER if has_other else CLIENT_CREATE_OBJECT_REQUIRED)
        resp.add_uint32(parent_id)
        resp.add_uint32(zone_id)
        resp.add_uint16(dc_id)
        resp.add_uint32(do_id)
        resp.add_bytes(dgi.get_remaining())
        self.send_datagram(resp)

    def send_go_get_lost(self, booted_index, booted_text):
        resp = Datagram()
        resp.add_uint16(CLIENT_GO_GET_LOST)
        resp.add_uint16(booted_index)
        resp.add_string16(booted_text.encode('utf-8'))
        self.send_datagram(resp)

    def annihilate(self):
        self.service.upstream.unsubscribe_all(self)

        for object in self.session_objects:
            #delete
            pass

    def send_datagram(self, dg: Datagram):
        print('sending...', dg.get_message())
        ToontownProtocol.send_datagram(self, dg)


class ClientAgentProtocol(MDUpstreamProtocol):
    def handle_datagram(self, dg, dgi):
        sender = dgi.get_channel()
        msgtype = dgi.get_uint16()

        print('unhandled', msgtype)


class ClientAgent(DownstreamMessageDirector, UpstreamServer, ChannelAllocator):
    downstream_protocol = ClientProtocol
    upstream_protocol = ClientAgentProtocol

    min_channel = config['ClientAgent.MIN_CHANNEL']
    max_channel = config['ClientAgent.MAX_CHANNEL']

    def __init__(self, loop):
        DownstreamMessageDirector.__init__(self, loop)
        UpstreamServer.__init__(self, loop)
        ChannelAllocator.__init__(self)

        self.dc_file = parse_dc_file('toon.dc')

        self.avatars_field = self.dc_file.namespace['Account']['ACCOUNT_AV_SET']

        self.loop.set_exception_handler(self._on_exception)

        self._context = 0

        print(self.dc_file.hash)

        self.listen_task = None

    def _on_exception(self, loop, context):
        print('err', context)

    async def run(self):
        await self.connect(config['MessageDirector.HOST'], config['MessageDirector.PORT'])
        self.listen_task = self.loop.create_task(self.listen(config['ClientAgent.HOST'], config['ClientAgent.PORT']))
        await self.route()

    def on_upstream_connect(self):
        pass

    def context(self):
        self._context = (self._context + 1) & 0xFFFFFFFF
        return self._context

    def process_datagram(self, participant: MDParticipant, dg: Datagram):
        print('process', dg.get_message().tobytes())
        DownstreamMessageDirector.process_datagram(self, participant, dg)


async def main():
    loop = asyncio.get_running_loop()
    service = ClientAgent(loop)
    await service.run()


if __name__ == '__main__':
    #import ssl
    #ClientAgent.SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
    #ClientAgent.SSL_CONTEXT.load_cert_chain('server.crt', keyfile='server.key')
    asyncio.run(main(), debug=True)

#Shared ciphers:EDH-RSA-DES-CBC3-SHA:EDH-DSS-DES-CBC3-SHA:DES-CBC3-SHA:IDEA-CBC-SHA:RC4-SHA:RC4-MD5
#CIPHER is EDH-RSA-DES-CBC3-SHA