#!/bin/sh

VERSION=6.9
BUILDDIR=$HOME/$VERSION/builds/openvix/developer/gb7252

cd "$HOME/$VERSION" || exit 1

make update

# $HOME/update-hbbtv-recipes

python3 $HOME/ipk_compare.py snapshot $HOME/before.json

cd "$BUILDDIR" || exit 1
source ./env.source
bitbake packagegroup-hbbtv2 || exit 1

python3 $HOME/ipk_compare.py snapshot $HOME/after.json

# show new files added to folder
python3 $HOME/ipk_compare.py compare $HOME/before.json $HOME/after.json

# python3 $HOME/collect_hbbtv_feed.py

# python3 $HOME/collect_hbbtv_feed.py --dry-run --extras
python3 $HOME/collect_hbbtv_feed.py --extras --index

# following done automatically by collect_hbbtv_feed.py, but usage shown here for infomation purposes
# python3 $HOME/recreate_feed.py $HOME/www/feed-static-6.0/static-cortexa15hf-neon-vfpv4

$HOME/static-tools/sync_static.sh

# preview
# $HOME/remove-hbbtv2-ipks "$BUILDDIR/tmp/deploy/ipk"

# real delete
$HOME/remove-hbbtv2-ipks "$BUILDDIR/tmp/deploy/ipk" --delete

cd
