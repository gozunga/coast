# Copyright 2026 Gozunga and Coast contributors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Version-gated controller-local bootstrap (Q4)."""

import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from urllib import request

from coast.common import engine_versions


BOOTSTRAP_METHOD = 'coolify-artisan-token-v1'
VERSION = engine_versions.build_version()
CONTAINER_TOKEN = '/var/www/html/storage/app/ssh/.coast-bootstrap-token'
# Load Laravel's console kernel without Tinker's automatic result printing.
# This is the Q4 exception, not a general-purpose engine access mechanism.
PHP = r"""<?php
try {
require '/var/www/html/vendor/autoload.php';
$app = require '/var/www/html/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
if (config('constants.coolify.version') !== '__COAST_BUILD_VERSION__') {
    exit(1);
}
umask(0077);
$path = '/var/www/html/storage/app/ssh/.coast-bootstrap-token';
if (is_link($path) || is_link($path . '.lock')) { exit(1); }
$lock = fopen($path . '.lock', 'c');
if (!$lock || !flock($lock, LOCK_EX)) { exit(1); }
// RootUserSeeder's email:rfc,dns rejects example.com (null MX) and
// offline addresses. Use the private first-boot credentials without DNS.
if (!App\Models\User::find(0)) {
    $username = env('ROOT_USERNAME');
    $email = env('ROOT_USER_EMAIL');
    $password = env('ROOT_USER_PASSWORD');
    if (!$username || !$email || !$password) { exit(1); }
    $user = (new App\Models\User)->forceFill([
        'id' => 0, 'name' => $username, 'email' => $email,
        'password' => Illuminate\Support\Facades\Hash::make($password),
    ]);
    $user->save();
}
$user = App\Models\User::findOrFail(0);
$team = App\Models\Team::find(0);
if ($team !== null && !$user->teams()->where('team_id', 0)->exists()) {
    $user->teams()->attach($team, ['role' => 'owner']);
}
App\Models\InstanceSettings::updateOrCreate(
    ['id' => 0], ['is_registration_enabled' => false]
);
$team = $user->teams()->where('teams.id', 0)
    ->wherePivot('role', 'owner')->firstOrFail();
session(['currentTeam' => $team]);
$name = 'coast-agent-bootstrap-v1';
if (!file_exists($path)) {
    // A lost plaintext token cannot be recovered from its DB hash.
    // Refuse to silently create a second token after interrupted creation.
    if ($user->tokens()->where('name', $name)->exists()) { exit(1); }
    $file = fopen($path, 'x');
    if (!$file) { exit(1); }
    $token = $user->createToken($name, ['root']);
    $plain = $token->plainTextToken . "\n";
    if (fwrite($file, $plain) !== strlen($plain)
        || !fflush($file) || !fsync($file)) {
        exit(1);
    }
    fclose($file);
}
} catch (\Throwable $e) { exit(1); }
""".replace('__COAST_BUILD_VERSION__', VERSION)


class BootstrapError(Exception):
    """Safe error without engine output or secret values."""


def docker(*args, payload=None):
    result = subprocess.run(
        ['docker', *args], input=payload, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=60, check=False)
    if result.returncode:
        raise BootstrapError('Local Docker operation failed.')


def read_token(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise BootstrapError('Token file must be owned and private '
                                 '(0600).')
        value = stream.read(256).strip()
    if not re.fullmatch(r'[0-9]+\|[A-Za-z0-9]{48}', value):
        raise BootstrapError('Invalid token file; manual recovery required.')
    return value


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BootstrapError('API redirects are not permitted.')


def enable_api(token, port):
    # No proxy or redirects: the credential must not leave the controller.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    base = 'http://127.0.0.1:%d/api/v1' % port
    headers = {'Authorization': 'Bearer ' + token}
    for endpoint, method in (('/enable', 'POST'), ('/version', 'GET')):
        req = request.Request(base + endpoint, headers=headers, method=method)
        with opener.open(req, timeout=30) as response:
            body = response.read(256)
            if endpoint == '/version' and body.strip() != VERSION.encode():
                raise BootstrapError('API version does not match the pin.')


def check_image(container):
    # Inspect the actual image tag before running any internal code.
    result = subprocess.run(
        ['docker', 'inspect', '--format', '{{.Config.Image}}', container],
        capture_output=True, timeout=30, check=False)
    image = result.stdout.decode().strip()
    if (result.returncode or not re.fullmatch(
            r'(?:docker.io/)?coollabsio/coolify:' + re.escape(VERSION),
            image)):
        raise BootstrapError('Only the baked stock App engine is allowed.')


def bootstrap(output, container='coolify', port=8000, token_payload=None,
              container_token=CONTAINER_TOKEN):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container):
        raise BootstrapError('Container name is invalid.')
    output = Path(output).absolute()
    parent = output.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or parent.resolve() != parent):
        raise BootstrapError('Output directory must be owned and private '
                             '(0700).')
    lock_fd = os.open(parent / '.bootstrap.lock',
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        check_image(container)
        if not os.path.lexists(output):
            payload = PHP if token_payload is None else token_payload
            docker('exec', '-i', '--user', '0', container, 'php',
                   payload=payload.encode())
            with tempfile.TemporaryDirectory(dir=parent) as temporary:
                staged = Path(temporary) / 'token'
                docker('cp', container + ':' + container_token, str(staged))
                os.chmod(staged, 0o600)
                read_token(staged)
                with staged.open('rb') as stream:
                    os.fsync(stream.fileno())
                os.rename(staged, output)
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        enable_api(read_token(output), port)
        # On failure, keep the container copy for a retry.
        docker('exec', '--user', '0', container, 'rm', '-f', container_token)
