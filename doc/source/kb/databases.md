---
orphan: true
---

# Databases

**Support-center draft. For James to review. Not published.**

A Coast database runs on a worker in your stack. It belongs to an app
group and environment. It is separate from an app or a template service.
Each database uses one service quota slot.

## Create a database

In the dashboard, open Project > App Hosting > Databases and select
Create database. Search the engine catalog, see the Versions column,
then select the engine row's Create database button. Enter Name, app
group, environment, worker and version. Open the database's Overview tab
to check status and wait for ACTIVE.

Name is the Coast name shown in Coast. Under Connection settings in the
Create database form, Initial database is the database created inside the
app engine server for your app (`--database`), and Username is the database
user for your app (`--username`). Both default to `app`; engines without
these settings do not show them. Coast generates a strong password and
stores it securely; there is no password input.

Run `openstack coast database engine list` to see what your cloud offers.
The catalog lists available versions and backup support. Use
`openstack coast database create --help` for the required options. Choose
an app group, environment and worker in the same stack. Use
`openstack coast database show` to wait for `ACTIVE`.
If creation fails, check `openstack coast database logs`.

## Connect an app

Place the app on the same worker and use the database's internal host.
After creation, project members can reveal the password with Show password
on the Overview tab or
`openstack coast database credentials show NAME --show-password`.
Replace `NAME` with the Coast name. Without `--show-password`, secrets are
masked. Revealed output, including the full connection URL, is secret;
keep it private. Ordinary show output omits passwords.

Store the URL with a hidden prompt, then redeploy the app:

```console
$ openstack coast env set --service web DATABASE_URL --secret --no-build-time
$ openstack coast deploy web
```

Enter the URL only at the prompt. Never paste it into a ticket or log.
The internal hostname is for the worker container network. It is not an
internet address.

## Allow external clients

Public access starts disabled. Use `openstack coast database set` to
select a public port and a restricted IPv4 source CIDR. Coast manages
the firewall rule. Use the returned public endpoint. IPv6 public access
is not supported. Public access does not itself enable database TLS.
Disable public access when you no longer need it.

## Back up and restore

Database backups require a Swift-compatible object-storage endpoint in the
cloud (for example, Swift or RGW with its Swift API enabled). They are not
verified end to end in 0.1.0; full verification is planned for 0.1.1.

Check backup support in the engine catalog. Save a policy with
`openstack coast database backup policy set`. Coast derives the engine
and container. Read it with `openstack coast database backup policy show`.
Schedules use UTC. Create a manual backup with
`openstack coast database backup create` and list backups with
`openstack coast database backup list`.

Use `openstack coast backup restore` with a backup ID. A `new_copy`
restore creates a database with the same engine, version and placement.
An overwrite needs the current database name as confirmation and makes a
safety backup first. Check the restore job before reconnecting the app.
A failed restore can leave partial data.

## Stop or delete

Use `openstack coast database stop` from `ACTIVE` to pause the database
and keep its data. Start it from `STOPPED` with
`openstack coast database start`. Restart requires `ACTIVE`.

In the dashboard, the delete modal shows "To delete, type `<name>` below".
Type Name, the Coast name shown in Coast, to confirm deletion.

`openstack coast database delete` destroys the database and its data
volumes. Its backups are also removed by cleanup. Download any backup
you need to keep before deleting. Deletion finishes when show reports
that the database is not found.

See the [database guide](../portal-guide.md#databases) for more detail.
