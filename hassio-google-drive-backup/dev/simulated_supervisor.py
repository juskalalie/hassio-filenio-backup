import asyncio
from asyncio.tasks import sleep
from datetime import timedelta
import random
import string
import io

from backup.config import Config, Version
from backup.time import Time
from aiohttp.web import (HTTPBadRequest, HTTPNotFound,
                         HTTPUnauthorized, Request, Response, get,
                         json_response, post, delete, FileResponse)
from injector import inject, singleton
from .base_server import BaseServer
from .ports import Ports
from typing import Any, Dict
from tests.helpers import all_addons, createBackupTar, parseBackupInfo

URL_MATCH_BACKUP_FULL = "^/backups/new/full$"
URL_MATCH_BACKUP_DELETE = "^/backups/.*$"
URL_MATCH_BACKUP_DOWNLOAD = "^/backups/.*/download$"
URL_MATCH_MISC_INFO = "^/info$"
URL_MATCH_CORE_API = "^/core/api.*$"
URL_MATCH_START_ADDON = "^/addons/.*/start$"
URL_MATCH_STOP_ADDON = "^/addons/.*/stop$"
URL_MATCH_ADDON_INFO = "^/addons/.*/info$"
URL_MATCH_SELF_OPTIONS = "^/addons/self/options$"

URL_MATCH_SNAPSHOT = "^/snapshots.*$"
URL_MATCH_BACKUPS = "^/backups.*$"
URL_MATCH_MOUNT = "^/mounts*$"


@singleton
class SimulatedSupervisor(BaseServer):
    @inject
    def __init__(self, config: Config, ports: Ports, time: Time):
        self._config = config
        self._time = time
        self._ports = ports
        self._auth_token = "test_header"
        self._backups: Dict[str, Any] = {}
        self._backup_data: Dict[str, bytearray] = {}
        self._backup_lock = asyncio.Lock()
        self._backup_inner_lock = asyncio.Lock()
        self._entities = {}
        self._events = []
        self._attributes = {}
        self._notification = None
        self._min_backup_size = 1024 * 1024 * 5
        self._max_backup_size = 1024 * 1024 * 5
        self._addon_slug = "self_slug"
        self._options = self.defaultOptions()
        self._username = "user"
        self._password = "pass"
        self._addons = all_addons.copy()
        self._super_version = Version(2023, 7)
        self._mounts = {
            'default_backup_mount': None,
            'mounts': [
                {
                    "name": "my_media_share",
                    "usage": "media",
                    "type": "cifs",
                    "server": "server.local",
                    "share": "media",
                    "state": "active"
                },
                {
                    "name": "my_backup_share",
                    "usage": "backup",
                    "type": "nfs",
                    "server": "server.local",
                    "share": "media",
                    "state": "active"
                }
            ]
        }

        self.installAddon(self._addon_slug, "Home Assistant Google drive Backup")
        self.installAddon("42", "The answer")
        self.installAddon("sgadg", "sdgsagsdgsggsd")

    def defaultOptions(self):
        return {
            "max_backups_in_ha": 4,
            "max_backups_in_filenio": 4,
            "days_between_backups": 3
        }

    def routes(self):
        return [
            post('/addons/{slug}/options', self._updateOptions),
            post("/core/api/services/persistent_notification/dismiss", self._dismissNotification),
            post("/core/api/services/persistent_notification/create", self._createNotification),
            post("/core/api/events/{name}", self._haEventUpdate),
            post("/core/api/states/{entity}", self._haStateUpdate),
            post('/auth', self._authenticate),
            get('/auth', self._authenticate),
            get('/info', self._miscInfo),
            get('/addons/self/info', self._selfInfo),
            get('/addons', self._allAddons),
            get('/addons/{slug}/info', self._addonInfo),

            post('/addons/{slug}/start', self._startAddon),
            post('/addons/{slug}/stop', self._stopAddon),
            get('/addons/{slug}/logo', self._logoAddon),
            get('/addons/{slug}/icon', self._logoAddon),

            get('/core/info', self._coreInfo),
            get('/supervisor/info', self._supervisorInfo),
            get('/supervisor/logs', self._supervisorLogs),
            get('/core/logs', self._coreLogs),
            get('/debug/insert/backup', self._debug_insert_backup),
            get('/debug/info', self._debugInfo),
            post("/debug/mounts", self._setMounts),

            get('/backups', self._getBackups),
            get('/mounts', self._getMounts),
            delete('/backups/{slug}', self._deletebackup),
            post('/backups/new/upload', self._uploadbackup),
            post('/backups/new/partial', self._newbackup),
            post('/backups/new/full', self._newbackup),
            get('/backups/new/full', self._newbackup),
            get('/backups/{slug}/download', self._backupDownload),
            get('/backups/{slug}/info', self._backupDetail),
            get('/debug/backups/lock', self._lock_backups),

            # TODO: remove once the api path is fully deprecated
            get('/snapshots', self._getSnapshots),
            post('/snapshots/{slug}/remove', self._deletebackup),
            post('/snapshots/new/upload', self._uploadbackup),
            post('/snapshots/new/partial', self._newbackup),
            post('/snapshots/new/full', self._newbackup),
            get('/snapshots/new/full', self._newbackup),
            get('/snapshots/{slug}/download', self._backupDownload),
            get('/snapshots/{slug}/info', self._backupDetail),
        ]

    def getEvents(self):
        return self._events.copy()

    def getEntity(self, entity):
        return self._entities.get(entity)

    def clearEntities(self):
        self._entities = {}

    def addon(self, slug):
        for addon in self._addons:
            if addon["slug"] == slug:
                return addon
        return None

    def getAttributes(self, attribute):
        return self._attributes.get(attribute)

    def getNotification(self):
        return self._notification

    def _formatErrorResponse(self, error: str) -> str:
        return json_response({'result': error})

    def _formatDataResponse(self, data: Any) -> Response:
        return json_response({'result': 'ok', 'data': data})

    async def toggleBlockBackup(self):
        if self._backup_lock.locked():
            self._backup_lock.release()
        else:
            await self._backup_lock.acquire()

    async def _verifyHeader(self, request) -> bool:
        if request.headers.get("Authorization", None) == "Bearer " + self._auth_token:
            return
        if request.headers.get("X-Supervisor-Token", None) == self._auth_token:
            return
        raise HTTPUnauthorized()

    async def _getSnapshots(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({'snapshots': list(self._backups.values())})

    async def _getBackups(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({'backups': list(self._backups.values())})

    async def _getMounts(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse(self._mounts)
    
    async def _setMounts(self, request: Request):
        self._mounts = await request.json()
        return self._formatDataResponse({})

    async def _stopAddon(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        for addon in self._addons:
            if addon.get("slug", "") == slug:
                if addon.get("state") == "started":
                    addon["state"] = "stopped"
                    return self._formatDataResponse({})
        raise HTTPBadRequest()

    async def _logoAddon(self, request: Request):
        await self._verifyHeader(request)
        return FileResponse('hassio-filenio-backup/backup/static/images/logo.png')

    async def _startAddon(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        for addon in self._addons:
            if addon.get("slug", "") == slug:
                if addon.get("state") != "started":
                    addon["state"] = "started"
                    return self._formatDataResponse({})
        raise HTTPBadRequest()

    async def _addonInfo(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        for addon in self._addons:
            if addon.get("slug", "") == slug:
                return self._formatDataResponse({
                    'boot': addon.get("boot"),
                    'watchdog': addon.get("watchdog"),
                    'state': addon.get("state"),
                })
        raise HTTPBadRequest()

    async def _supervisorInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse(
            {
                'version': str(self._super_version)
            }
        )

    async def _allAddons(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse(
            {
                "addons": list(self._addons).copy()
            }
        )

    async def _supervisorLogs(self, request: Request):
        await self._verifyHeader(request)
        return Response(body=self.generate_random_text(20, 10, 20))

    def generate_random_text(self, line_count, min_words=5, max_words=10):
        lines = []
        log_levels = ["WARN", "WARNING", "INFO", "ERROR", "DEBUG"]
        for _ in range(line_count):
            level = random.choice(log_levels)
            word_count = random.randint(min_words, max_words)
            words = [random.choice(string.ascii_lowercase) for _ in range(word_count)]
            line = level + " " + ' '.join(''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(3, 10))) for _ in words)
            lines.append(line)
        return '\n'.join(lines)

    async def _coreLogs(self, request: Request):
        await self._verifyHeader(request)
        return Response(body="Core Log line 1\nCore Log Line 2")

    async def _coreInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse(
            {
                "version": "1.3.3.7",
                "last_version": "1.3.3.8",
                "machine": "VS Dev",
                "ip_address": "127.0.0.1",
                "arch": "x86",
                "image": "image",
                "custom": "false",
                "boot": "true",
                "port": self._ports.server,
                "ssl": "false",
                "watchdog": "what is this",
                "wait_boot": "so many arguments"
            }
        )

    async def _internalNewBackup(self, request: Request, input_json, date=None, verify_header=True) -> str:
        async with self._backup_lock:
            async with self._backup_inner_lock:
                if 'wait' in input_json:
                    await sleep(input_json['wait'])
                if verify_header:
                    await self._verifyHeader(request)
                slug = self.generateId(8)
                password = input_json.get('password', None)
                data = createBackupTar(
                    slug,
                    input_json.get('name', "Default name"),
                    date=date or self._time.now(),
                    padSize=int(random.uniform(self._min_backup_size, self._max_backup_size)),
                    included_folders=input_json.get('folders', None),
                    included_addons=input_json.get('addons', None),
                    password=password)
                backup_info = parseBackupInfo(data)
                self._backups[slug] = backup_info
                self._backup_data[slug] = bytearray(data.getbuffer())
                return slug

    async def createBackup(self, input_json, date=None):
        return await self._internalNewBackup(None, input_json, date=date, verify_header=False)

    async def _newbackup(self, request: Request):
        if self._backup_lock.locked():
            raise HTTPBadRequest()
        input_json = await request.json()
        task = asyncio.shield(asyncio.create_task(self._internalNewBackup(request, input_json)))
        return self._formatDataResponse({"slug": await task})
    
    async def _lock_backups(self, request: Request):
        await self._backup_lock.acquire()
        return self._formatDataResponse({"message": "locked"})

    async def _uploadbackup(self, request: Request):
        await self._verifyHeader(request)
        try:
            reader = await request.multipart()
            contents = await reader.next()
            received_bytes = bytearray()
            while True:
                chunk = await contents.read_chunk()
                if not chunk:
                    break
                received_bytes.extend(chunk)
            info = parseBackupInfo(io.BytesIO(received_bytes))
            self._backups[info['slug']] = info
            self._backup_data[info['slug']] = received_bytes
            return self._formatDataResponse({"slug": info['slug']})
        except Exception as e:
            print(str(e))
            return self._formatErrorResponse("Bad backup")

    async def _deletebackup(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        if slug not in self._backups:
            raise HTTPNotFound()
        del self._backups[slug]
        del self._backup_data[slug]
        return self._formatDataResponse("deleted")

    async def _backupDetail(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        if slug not in self._backups:
            raise HTTPNotFound()
        return self._formatDataResponse(self._backups[slug])

    async def _backupDownload(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        if slug not in self._backup_data:
            raise HTTPNotFound()
        return self.serve_bytes(request, self._backup_data[slug])

    async def _selfInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({
            "webui": "http://some/address",
            'ingress_url': "fill me in later",
            "slug": self._addon_slug,
            "options": self._options
        })

    async def _debugInfo(self, request: Request):
        return self._formatDataResponse({
            "config": {
                "   webui": "http://some/address",
                'ingress_url': "fill me in later",
                "slug": self._addon_slug,
                "options": self._options
            }
        })

    async def _miscInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({
            "supervisor": "super version",
            "homeassistant": "ha version",
            "hassos": "hassos version",
            "hostname": "hostname",
            "machine": "machine",
            "arch": "Arch",
            "supported_arch": "supported arch",
            "channel": "channel"
        })

    def installAddon(self, slug, name, version="v1.0", boot=True, started=True):
        self._addons.append({
            "name": 'Name for ' + name,
            "slug": slug,
            "description": slug + " description",
            "version": version,
            "watchdog": False,
            "boot": "auto" if boot else "manual",
            "logo": True,
            "ingress_entry": "/api/hassio_ingress/" + slug,
            "state": "started" if started else "stopped"
        })

    async def _authenticate(self, request: Request):
        await self._verifyHeader(request)
        input_json = await request.json()
        if input_json.get("username") != self._username or input_json.get("password") != self._password:
            raise HTTPBadRequest()
        return self._formatDataResponse({})

    async def _updateOptions(self, request: Request):
        slug = request.match_info.get('slug')

        if slug == "self":
            await self._verifyHeader(request)
            self._options = (await request.json())['options'].copy()
        else:
            self.addon(slug).update(await request.json())
        return self._formatDataResponse({})

    async def _haStateUpdate(self, request: Request):
        await self._verifyHeader(request)
        entity = request.match_info.get('entity')
        json = await request.json()
        self._entities[entity] = json['state']
        self._attributes[entity] = json['attributes']
        return Response()

    async def _haEventUpdate(self, request: Request):
        await self._verifyHeader(request)
        name = request.match_info.get('name')
        self._events.append((name, await request.json()))
        return Response()

    async def _createNotification(self, request: Request):
        await self._verifyHeader(request)
        notification = await request.json()
        print("Created notification with: {}".format(notification))
        self._notification = notification.copy()
        return Response()

    async def _dismissNotification(self, request: Request):
        await self._verifyHeader(request)
        print("Dismissed notification with: {}".format(await request.json()))
        self._notification = None
        return Response()

    async def _debug_insert_backup(self, request: Request) -> Response:
        days_back = int(request.query.get("days"))
        date = self._time.now() - timedelta(days=days_back)
        name = date.strftime("Full Backup %Y-%m-%d %H:%M-%S")
        wait = int(request.query.get("wait", 0))
        slug = await self._internalNewBackup(request, {'name': name, 'wait': wait}, date=date, verify_header=False)
        return self._formatDataResponse({'slug': slug})
