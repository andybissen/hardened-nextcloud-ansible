<?php
// Reconciles Nextcloud's persisted config.php DB credentials with the
// Postgres role this playbook just (re)created. Nextcloud only ever
// auto-configures dbuser/dbpassword from POSTGRES_USER/
// POSTGRES_PASSWORD_FILE at its very first install - after that it reads
// them from this file forever, never re-consulting those env vars again.
// A Postgres major upgrade wipes and recreates the Postgres role while
// deliberately leaving nc_data (and this file) untouched, so if
// config.php was ever written with different credentials than what's
// configured now, the freshly-created role wouldn't match what Nextcloud
// tries to connect as. Forcing them to match here makes that failure
// mode structurally impossible rather than hoping the two already agree.
$path = '/var/www/html/config/config.php';
$secret = '/run/secrets/postgres_password';
include $path;
$CONFIG['dbuser'] = 'nextcloud';
$CONFIG['dbpassword'] = trim(file_get_contents($secret));
file_put_contents($path, "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n");
echo "config.php dbuser/dbpassword reconciled to current Postgres credentials\n";
