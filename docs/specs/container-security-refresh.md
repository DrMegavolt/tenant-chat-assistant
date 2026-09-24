# Python runtime operating-system security refresh

## Trigger

The CI container gate reports a fixed HIGH or CRITICAL vulnerability in an
operating-system package shipped by the digest-pinned Python runtime base.

## Expected result

The final API and embedding image stages install available Debian security
updates during the image build while retaining the reviewed base-image digest.
The immutable-image contract rejects a Python runtime stage that omits this
refresh, and the image smoke tests continue to run the services as uid 10001.

## Tenant boundary

The refresh changes only operating-system packages below the application. It
does not change tenant identity, authorization, storage keys, database roles, or
request routing.

## Failure behavior

Package-index or security-upgrade failure stops the image build. CI continues to
block any remaining fixed HIGH or CRITICAL image finding rather than adding a
vulnerability waiver.

## Exclusions

This does not replace periodic base-digest review, dependency locking, or the
Trivy scan. Builder-only packages are not copied into the final runtime image.
