import logging

from django.conf import settings
from django.db import models
from rest_framework.serializers import ValidationError

from registry import publish_release, RegistryException
from api.utils import dict_diff
from api.models import UuidAuditedModel, log_event
from scheduler import KubeHTTPException

logger = logging.getLogger(__name__)


class Release(UuidAuditedModel):
    """
    Software release deployed by the application platform

    Releases contain a :class:`Build` and a :class:`Config`.
    """

    owner = models.ForeignKey(settings.AUTH_USER_MODEL)
    app = models.ForeignKey('App')
    version = models.PositiveIntegerField()
    summary = models.TextField(blank=True, null=True)

    config = models.ForeignKey('Config')
    build = models.ForeignKey('Build', null=True)

    class Meta:
        get_latest_by = 'created'
        ordering = ['-created']
        unique_together = (('app', 'version'),)

    def __str__(self):
        return "{0}-v{1}".format(self.app.id, self.version)

    @property
    def image(self):
        # return image if it is already in the registry, test host and then host + port
        if (
            self.build.image.startswith(settings.REGISTRY_HOST) or
            self.build.image.startswith(settings.REGISTRY_URL)
        ):
            # strip registry information off first
            image = self.build.image.replace('{}/'.format(settings.REGISTRY_URL), '')
            return image.replace('{}/'.format(settings.REGISTRY_HOST), '')

        if not self.build.dockerfile:
            # Deis Pull
            if not self.build.sha:
                return '{}:v{}'.format(self.app.id, str(self.version))

            # Build Pack
            return self.build.image

        # DockerFile
        return '{}:git-{}'.format(self.app.id, str(self.build.sha))

    def new(self, user, config, build, summary=None, source_version='latest'):
        """
        Create a new application release using the provided Build and Config
        on behalf of a user.

        Releases start at v1 and auto-increment.
        """
        # construct fully-qualified target image
        new_version = self.version + 1
        # create new release and auto-increment version
        release = Release.objects.create(
            owner=user, app=self.app, config=config,
            build=build, version=new_version, summary=summary
        )

        try:
            release.publish()
        except EnvironmentError as e:
            # If we cannot publish this app, just log and carry on
            log_event(self.app, e)
            pass
        except RegistryException as e:
            log_event(self.app, e)
            # Uses ValidationError to get return of 400 up in views
            raise ValidationError({'detail': str(e)})

        return release

    def publish(self, source_version='latest'):
        if self.build is None:
            raise EnvironmentError('No build associated with this release to publish')

        source_image = self.build.image
        # return image if it is already in the registry, test host and then host + port
        if (
            source_image.startswith(settings.REGISTRY_HOST) or
            source_image.startswith(settings.REGISTRY_URL)
        ):
            log_event(self.app, '{} already exists in the target registry. Using this image for release {} of app {}'.format(source_image, self.version, self.app))  # noqa
            return

        # add tag if it was not provided
        if ':' not in source_image:
            source_tag = 'git-{}'.format(self.build.sha) if self.build.sha else source_version
            source_image = "{}:{}".format(source_image, source_tag)

        # If the build has a SHA, assume it's from deis-builder and in the deis-registry already
        if not self.build.dockerfile and not self.build.sha:
            deis_registry = bool(self.build.sha)
            publish_release(source_image, self.image, deis_registry)

    def previous(self):
        """
        Return the previous Release to this one.

        :return: the previous :class:`Release`, or None
        """
        releases = self.app.release_set
        if self.pk:
            releases = releases.exclude(pk=self.pk)

        try:
            # Get the Release previous to this one
            prev_release = releases.latest()
        except Release.DoesNotExist:
            prev_release = None
        return prev_release

    def rollback(self, user, version):
        if version < 1:
            raise EnvironmentError('version cannot be below 0')

        summary = "{} rolled back to v{}".format(user, version)
        prev = self.app.release_set.get(version=version)
        new_release = self.new(
            user,
            build=prev.build,
            config=prev.config,
            summary=summary,
            source_version='v{}'.format(version)
        )

        try:
            if self.build is not None:
                self.app.deploy(user, new_release)
            return new_release
        except Exception:
            if 'new_release' in locals():
                new_release.delete()
            raise

    def delete(self, *args, **kwargs):
        """Delete release DB record and any RCs from the affect release"""
        try:
            labels = {
                'app': self.app.id,
                'version': 'v{}'.format(self.version)
            }
            controllers = self._scheduler._get_rcs(self.app.id, labels=labels)
            for controller in controllers.json()['items']:
                self._scheduler._delete_rc(self.app.id, controller['metadata']['name'])
        except KubeHTTPException as e:
            # 404 means they were already cleaned up
            if e.status_code is not 404:
                # Another problem came up
                message = 'Could not to cleanup RCs for release {}'.format(self.version)
                log_event(self.app, message)
                logger.warning(message + ' - ' + str(e))
        finally:
            super(Release, self).delete(*args, **kwargs)

    def save(self, *args, **kwargs):  # noqa
        if not self.summary:
            self.summary = ''
            prev_release = self.previous()
            # compare this build to the previous build
            old_build = prev_release.build if prev_release else None
            old_config = prev_release.config if prev_release else None
            # if the build changed, log it and who pushed it
            if self.version == 1:
                self.summary += "{} created initial release".format(self.app.owner)
            elif self.build != old_build:
                if self.build.sha:
                    self.summary += "{} deployed {}".format(self.build.owner, self.build.sha[:7])
                else:
                    self.summary += "{} deployed {}".format(self.build.owner, self.build.image)
            # if the config data changed, log the dict diff
            if self.config != old_config:
                dict1 = self.config.values
                dict2 = old_config.values if old_config else {}
                diff = dict_diff(dict1, dict2)
                # try to be as succinct as possible
                added = ', '.join(k for k in diff.get('added', {}))
                added = 'added ' + added if added else ''
                changed = ', '.join(k for k in diff.get('changed', {}))
                changed = 'changed ' + changed if changed else ''
                deleted = ', '.join(k for k in diff.get('deleted', {}))
                deleted = 'deleted ' + deleted if deleted else ''
                changes = ', '.join(i for i in (added, changed, deleted) if i)
                if changes:
                    if self.summary:
                        self.summary += ' and '
                    self.summary += "{} {}".format(self.config.owner, changes)
                # if the limits changed (memory or cpu), log the dict diff
                changes = []
                old_mem = old_config.memory if old_config else {}
                diff = dict_diff(self.config.memory, old_mem)
                if diff.get('added') or diff.get('changed') or diff.get('deleted'):
                    changes.append('memory')
                old_cpu = old_config.cpu if old_config else {}
                diff = dict_diff(self.config.cpu, old_cpu)
                if diff.get('added') or diff.get('changed') or diff.get('deleted'):
                    changes.append('cpu')
                if changes:
                    changes = 'changed limits for '+', '.join(changes)
                    self.summary += "{} {}".format(self.config.owner, changes)
                # if the tags changed, log the dict diff
                changes = []
                old_tags = old_config.tags if old_config else {}
                diff = dict_diff(self.config.tags, old_tags)
                # try to be as succinct as possible
                added = ', '.join(k for k in diff.get('added', {}))
                added = 'added tag ' + added if added else ''
                changed = ', '.join(k for k in diff.get('changed', {}))
                changed = 'changed tag ' + changed if changed else ''
                deleted = ', '.join(k for k in diff.get('deleted', {}))
                deleted = 'deleted tag ' + deleted if deleted else ''
                changes = ', '.join(i for i in (added, changed, deleted) if i)
                if changes:
                    if self.summary:
                        self.summary += ' and '
                    self.summary += "{} {}".format(self.config.owner, changes)
            if not self.summary:
                if self.version == 1:
                    self.summary = "{} created the initial release".format(self.owner)
                else:
                    self.summary = "{} changed nothing".format(self.owner)
        super(Release, self).save(*args, **kwargs)
