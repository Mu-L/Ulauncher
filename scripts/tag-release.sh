#!/usr/bin/env bash

#############################
# Update the version in the setup.cfg, make a commit and tag it
#############################
tag-release() {
    # Args:
    # $1 version

    export VERSION=$1
    if [ -z "$VERSION" ]; then
        echo "First argument should be version"
        exit 1
    fi

    # Update version in setup.cfg, commit and set git tag
    ./setup.py setopt --command metadata --option version --set-value $VERSION
    git commit setup.cfg -m "Release $VERSION"
    git tag $VERSION
}
