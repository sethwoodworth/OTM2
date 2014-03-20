from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import hashlib
from functools import partial

from django.contrib.gis.db import models
from django.contrib.gis.geos import GEOSGeometry

from django.forms.models import model_to_dict
from django.utils.translation import ugettext as trans
from django.dispatch import receiver
from django.db.models import OneToOneField
from django.db.models.signals import post_save
from django.db.models.fields import FieldDoesNotExist
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, connection, transaction

from treemap.units import (is_convertible, is_convertible_or_formattable,
                           get_display_value, get_units, get_unit_name)
from treemap.util import leaf_subclasses


def model_hasattr(obj, name):
    # hasattr will not work here because it
    # just calls getattr and looks for exceptions
    # not differentiating between DoesNotExist
    # and AttributeError
    try:
        getattr(obj, name)
        return True
    except ObjectDoesNotExist:
        return True
    except AttributeError:
        return False


def get_id_sequence_name(model_class):
    """
    Takes a django model class and returns the name of the autonumber
    sequence for the id field.
    Tree => 'treemap_tree_id_seq'
    Plot => 'treemap_mapfeature_id_seq'
    """
    if isinstance(model_class._meta.pk, OneToOneField):
        # Model uses multi-table inheritance (probably a MapFeature subclass)
        model_class = model_class._meta.pk.related.parent_model

    table_name = model_class._meta.db_table
    pk_field = model_class._meta.pk
    # django fields only have a truthy db_column when it is
    # overriding the default
    pk_column_name = pk_field.db_column or pk_field.name
    id_seq_name = "%s_%s_seq" % (table_name, pk_column_name)
    return id_seq_name


def _reserve_model_id(model_class):
    """
    queries the database to get id from the audit id sequence.
    this is used to reserve an id for a record that hasn't been
    created yet, in order to make references to that record.
    """
    try:
        id_seq_name = get_id_sequence_name(model_class)
        cursor = connection.cursor()
        cursor.execute("select nextval('%s');" % id_seq_name)
        results = cursor.fetchone()
        model_id = results[0]
        assert(type(model_id) in [int, long])
    except:
        msg = "There was a database error while retrieving a unique audit ID."
        raise IntegrityError(msg)

    return model_id


@transaction.commit_on_success
def approve_or_reject_audits_and_apply(audits, user, approved):
    """
    Approve or reject a series of audits. This method provides additional
    logic to make sure that creation audits (id audits) get applied last.

    It also performs 'Plot' audits before 'Tree' audits, since a tree
    cannot be made concrete until the corresponding plot has been created

    This method runs inside of a transaction, so if any applications fail
    we can bail without an inconsistent state
    """
    model_order = ['Plot', 'Tree']
    id_audits = []

    for audit in audits:
        if audit.field == 'id':
            id_audits.append(audit)
        else:
            approve_or_reject_audit_and_apply(audit, user, approved)

    for model in model_order:
        remaining = []
        for audit in id_audits:
            if audit.model == model:
                approve_or_reject_audit_and_apply(audit, user, approved)
            else:
                remaining.append(audit)
        id_audits = list(remaining)

    for audit in remaining:
        approve_or_reject_audit_and_apply(audit, user, approved)


def add_default_permissions(instance, roles=None, models=None):
    if roles is None:
        roles = Role.objects.filter(instance=instance)
    if models is None:
        models = leaf_subclasses(Authorizable)

    for role in roles:
        _add_default_permissions(models, role, instance)


def _add_default_permissions(models, role, instance):
    """
    Create FieldPermission entries for role using its default permission level.
    Make an entry for every tracked field of given models, as well as UDFs of
    given instance.
    """
    from udf import UserDefinedFieldDefinition

    perms = []
    for Model in models:
        mobj = Model(instance=instance)

        model_name = mobj._model_name
        udfs = [udf.canonical_name for udf in
                UserDefinedFieldDefinition.objects.filter(
                    instance=instance, model_type=model_name)]

        model_fields = set(mobj.tracked_fields + udfs)

        for field_name in model_fields:
            perms.append({
                'model_name': model_name,
                'field_name': field_name,
                'role': role,
                'instance': role.instance
            })

    existing = FieldPermission.objects.filter(role=role, instance=instance)
    if existing.exists():
        for perm in perms:
            perm['defaults'] = {'permission_level': role.default_permission}
            FieldPermission.objects.get_or_create(**perm)
    else:
        perms = [FieldPermission(**perm) for perm in perms]
        for perm in perms:
            perm.permission_level = role.default_permission
        FieldPermission.objects.bulk_create(perms)


def approve_or_reject_existing_edit(audit, user, approved):
    """
    Approve or reject a given audit that has already been
    applied

    audit    - Audit record to update
    user     - The user who is approving or rejecting
    approved - True to generate an approval, False to
               revert the change

    Note that reverting is done outside of the audit system
    to make it appear as if the change had never happend
    """

    # If the ref has already been set, this audit has
    # already been accepted or rejected so we can't do anything
    if audit.ref:
        raise Exception('This audit has already been approved or rejected')

    # If this is a 'pending' audit, you must use the pending system
    if audit.requires_auth:
        raise Exception("This audit is pending, so it can't be approved")

    # this audit will be attached to the main audit via
    # the refid
    review_audit = Audit(model=audit.model, model_id=audit.model_id,
                         instance=audit.instance, field=audit.field,
                         previous_value=audit.previous_value,
                         current_value=audit.current_value,
                         user=user)

    # Regardless of what we're doing, we need to make sure
    # 'user' is authorized to approve this edit
    _verify_user_can_apply_audit(audit, user)

    TheModel = _get_auditable_class(audit.model)

    # If we want to 'review approve' this audit nothing
    # happens to the model, we're just saying "looks good!"
    if approved:
        review_audit.action = Audit.Type.ReviewApprove
    else:
        # If we are 'review rejecting' this audit we want to revert
        # the change
        #
        # note that audits cannot be pending before they have been
        # reviewed. That means that all of these audits are
        # concrete and thus can be reverted.
        review_audit.action = Audit.Type.ReviewReject

        # If the audit that we're reverting is an 'id' audit
        # it reverts the *entire* object (i.e. deletes it)
        #
        # This leads to an awkward sitution that must be handled
        # elsewhere where there are audits that appear to have
        # been applied but are on objects that have been
        # deleted.
        try:
            obj = TheModel.objects.get(pk=audit.model_id)

            # Delete the object, outside of the audit system
            if audit.field == 'id':
                models.Model.delete(obj)
            else:
                # For non-id fields we want to know if this is
                # the most recent audit on the field. If it isn't
                # the most recent then rejecting this audit doesn't
                # actually change the data
                most_recent_audits = Audit.objects\
                                          .filter(model=audit.model,
                                                  model_id=audit.model_id,
                                                  instance=audit.instance,
                                                  field=audit.field)\
                                          .order_by('-created')

                is_most_recent_audit = False
                try:
                    most_recent_audit_pk = most_recent_audits[0].pk
                    is_most_recent_audit = most_recent_audit_pk == audit.pk

                    if is_most_recent_audit:
                        obj.apply_change(audit.field,
                                         audit.clean_previous_value)
                        models.Model.save(obj)
                except IndexError:
                    pass
        except ObjectDoesNotExist:
            # As noted above, this is okay. Just mark the audit as
            # rejected
            pass

    review_audit.save()
    audit.ref = review_audit
    audit.save()

    return review_audit


def approve_or_reject_audit_and_apply(audit, user, approved):
    """
    Approve or reject a given audit and apply it to the
    underlying model if "approved" is True

    audit    - Audit record to apply
    user     - The user who is approving or rejecting
    approved - True to generate an approval, False to
               generate a rejection
    """

    # If the ref has already been set, this audit has
    # already been accepted or rejected so we can't do anything
    if audit.ref:
        raise Exception('This audit has already been approved or rejected')

    # Regardless of what we're doing, we need to make sure
    # 'user' is authorized to approve this edit
    _verify_user_can_apply_audit(audit, user)

    # Create an additional audit record to track the act of
    # the privileged user applying either PendingApprove or
    # pendingReject to the original audit.
    review_audit = Audit(model=audit.model, model_id=audit.model_id,
                         instance=audit.instance, field=audit.field,
                         previous_value=audit.previous_value,
                         current_value=audit.current_value,
                         user=user)

    TheModel = _get_auditable_class(audit.model)
    if approved:
        review_audit.action = Audit.Type.PendingApprove

        # use a try/catch to determine if the is a pending insert
        # or a pending update
        try:
            obj = TheModel.objects.get(pk=audit.model_id)
            obj.apply_change(audit.field,
                             audit.clean_current_value)
            # TODO: consider firing the save signal here
            # save this object without triggering any kind of
            # UserTrackable actions. There is no straightforward way to
            # call save on the object's parent here.

            obj.save_base()

        except ObjectDoesNotExist:
            if audit.field == 'id':
                _process_approved_pending_insert(TheModel, user, audit)

    else:  # Reject
        review_audit.action = Audit.Type.PendingReject

        if audit.field == 'id':
            related_audits = get_related_audits(audit)

            for related_audit in related_audits:
                related_audit.ref = None
                related_audit.save()
                approve_or_reject_audit_and_apply(related_audit, user, False)

    review_audit.save()
    audit.ref = review_audit
    audit.save()

    return review_audit


def _process_approved_pending_insert(model_class, user, audit):
    # get all of the audits
    obj = model_class(pk=audit.model_id)
    if model_hasattr(obj, 'instance'):
        obj.instance = audit.instance

    approved_audits = get_related_audits(audit, approved_only=True)

    for approved_audit in approved_audits:
        obj.apply_change(approved_audit.field,
                         approved_audit.clean_current_value)

    obj.validate_foreign_keys_exist()
    obj.save_base()


def get_related_audits(insert_audit, approved_only=False):
    """
    Takes a pending insert audit and retrieves all *other*
    records that were part of that insert.
    """
    related_audits = Audit.objects.filter(instance=insert_audit.instance,
                                          model_id=insert_audit.model_id,
                                          model=insert_audit.model,
                                          action=Audit.Type.Insert)\
                                  .exclude(pk=insert_audit.pk)
    if approved_only:
        related_audits = related_audits.filter(
            ref__action=Audit.Type.PendingApprove)

    return related_audits


def _verify_user_can_apply_audit(audit, user):
    """
    Make sure that user has "write direct" permissions
    for the given audit's fields.

    If the model is a udf collection, verify the user has
    write directly permission on the UDF
    """
    # This comingling here isn't really great...
    # However it allows us to have a pretty external interface in that
    # UDF collections can have a single permission based on the original
    # model, instead of having to assign a bunch of new ones.
    from udf import UserDefinedFieldDefinition

    if audit.model.startswith('udf:'):
        udf = UserDefinedFieldDefinition.objects.get(pk=audit.model[4:])
        field = 'udf:%s' % udf.name
        model = udf.model_type
    else:
        field = audit.field
        model = audit.model

    perms = user.get_instance_permissions(audit.instance,
                                          model_name=model)

    foundperm = False
    for perm in perms:
        if perm.field_name == field:
            if perm.permission_level == FieldPermission.WRITE_DIRECTLY:
                foundperm = True
                break
            else:
                raise AuthorizeException(
                    "User %s can't edit field %s on model %s" %
                    (user, field, model))

    if not foundperm:
        raise AuthorizeException(
            "User %s can't edit field %s on model %s (No permissions found)" %
            (user, field, model))


class UserTrackingException(Exception):
    pass


class Dictable(object):
    def as_dict(self):
        return model_to_dict(self, fields=[field.name for field in
                                           self._meta.fields])

    @property
    def hash(self):
        values = ['%s:%s' % (k, v) for (k, v) in self.as_dict().iteritems()]

        return hashlib.md5('|'.join(values)).hexdigest()


class UserTrackable(Dictable):
    def __init__(self, *args, **kwargs):
        self._do_not_track = set(['instance'])
        super(UserTrackable, self).__init__(*args, **kwargs)
        self.populate_previous_state()

    def apply_change(self, key, orig_value):
        # TODO: if a field has a default value, don't
        # set the original value when the original value
        # is none, set it to the default value of the field.
        setattr(self, key, orig_value)

    def _fields_required_for_create(self):
        return [field for field in self._meta.fields
                if (not field.null and
                    not field.blank and
                    not field.primary_key and
                    not field.name in self._do_not_track)]

    @property
    def tracked_fields(self):
        return [field.name
                for field
                in self._meta.fields
                if field.name not in self._do_not_track]

    def _updated_fields(self):
        updated = {}
        d = self.as_dict()
        for key in d:
            if key not in self._do_not_track:
                old = self._previous_state.get(key, None)
                new = d.get(key, None)

                if new != old:
                    updated[key] = [old, new]

        return updated

    def fields_were_updated(self):
        return len(self._updated_fields()) > 0

    @property
    def _model_name(self):
        return self.__class__.__name__

    def delete_with_user(self, user, *args, **kwargs):
        models.Model.delete(self, *args, **kwargs)
        self._previous_state = {}

    def save_with_user(self, user, *args, **kwargs):
        models.Model.save(self, *args, **kwargs)
        self.populate_previous_state()

    def save(self, *args, **kwargs):
        raise UserTrackingException(
            'All changes to %s objects must be saved via "save_with_user"' %
            (self._model_name))

    def delete(self, *args, **kwargs):
        raise UserTrackingException(
            'All deletes to %s objects must be saved via "delete_with_user"' %
            (self._model_name))

    def fields(self):
        return self.as_dict().keys()

    def populate_previous_state(self):
        """
        A helper method for setting up a previous state dictionary
        without the elements that should remained untracked
        """
        if self.pk is None:
            # User created the object as "MyObj(field1=...,field2=...)"
            # the saved state will include these changes but the actual
            # "initial" state is empty so we clear it here
            self._previous_state = {}
        else:
            self._previous_state = {k: v for k, v in self.as_dict().iteritems()
                                    if k not in self._do_not_track}

    def get_pending_fields(self, user=None):
        """
        Get a list of fields that are currently being updated which would
        require a pending edit.

        Should return a list-like object. An empty list is a no-op.

        Note: Since Authorizable doesn't control what happens to "pending" or
        "write with audit" fields it is the subclasses responsibility to
        handle the difference. A naive implementation (i.e. just subclassing
        Authorizable) treats "write with audit" that same as "write directly"
        """
        return []


class FieldPermission(models.Model):
    model_name = models.CharField(max_length=255)
    field_name = models.CharField(max_length=255)
    role = models.ForeignKey('Role')
    instance = models.ForeignKey('Instance')

    NONE = 0
    READ_ONLY = 1
    WRITE_WITH_AUDIT = 2
    WRITE_DIRECTLY = 3
    choices = (
        # reserving zero in case we want
        # to create a "null-permission" later
        (NONE, "None"),
        (READ_ONLY, "Read Only"),
        (WRITE_WITH_AUDIT, "Write with Audit"),
        (WRITE_DIRECTLY, "Write Directly"))
    permission_level = models.IntegerField(choices=choices, default=NONE)

    class Meta:
        unique_together = ('model_name', 'field_name', 'role', 'instance')

    def __unicode__(self):
        return "%s.%s - %s" % (self.model_name, self.field_name, self.role)

    @property
    def allows_reads(self):
        return self.permission_level >= self.READ_ONLY

    @property
    def allows_writes(self):
        return self.permission_level >= self.WRITE_WITH_AUDIT

    @property
    def display_field_name(self):
        if self.field_name.startswith('udf:'):
            base_name = self.field_name[4:]
        else:
            base_name = self.field_name

        return base_name.replace('_', ' ').title()

    def clean(self):
        try:
            cls = _get_authorizable_class(self.model_name)
            cls._meta.get_field_by_name(self.field_name)
            assert issubclass(cls, Authorizable)
        except KeyError:
            raise ValidationError("Model '%s' does not exist." %
                                  self.model_name)
        except FieldDoesNotExist as e:
            raise ValidationError("%s: Model '%s' does not have field '%s'"
                                  % (e.__class__.__name__,
                                     self.model_name,
                                     self.field_name))
        except AssertionError as e:
            raise ValidationError("%s: '%s' is not an Authorizable model. "
                                  "FieldPermissions can only be set "
                                  "on Authorizable models." %
                                  (e.__class__.__name__,
                                   self.model_name))

    def save(self, *args, **kwargs):
        self.full_clean()
        super(FieldPermission, self).save(*args, **kwargs)


class Role(models.Model):
    name = models.CharField(max_length=255)
    instance = models.ForeignKey('Instance', null=True, blank=True)
    default_permission = models.IntegerField(choices=FieldPermission.choices,
                                             default=FieldPermission.NONE)
    rep_thresh = models.IntegerField()

    @property
    def tree_permissions(self):
        return self.model_permissions('Tree')

    @property
    def plot_permissions(self):
        return self.model_permissions('Plot')

    def model_permissions(self, model):
        return self.fieldpermission_set.filter(model_name=model)

    def __unicode__(self):
        return '%s (%s)' % (self.name, self.pk)


class AuthorizeException(Exception):
    def __init__(self, name):
        super(Exception, self).__init__(name)


class AuthorizableQuerySet(models.query.QuerySet):

    def limit_fields_by_user(self, instance, user):
        """
        Filters a queryset based on what field permissions a user has on the
        model type.
        """
        perms = user.get_instance_permissions(instance,
                                              self.model.__name__)\
                    .values_list('field_name', flat=True)

        if perms:
            return self.values(*perms)
        else:
            return self.none()


class AuthorizableManager(models.GeoManager):
    def get_query_set(self):
        return AuthorizableQuerySet(self.model, using=self._db)


class Authorizable(UserTrackable):
    """
    Determines whether or not a user can save based on the
    edits they have attempted to make.
    """

    def __init__(self, *args, **kwargs):
        super(Authorizable, self).__init__(*args, **kwargs)

        self._has_been_masked = False

    def _get_perms_set(self, user, direct_only=False):

        try:
            perms = user.get_instance_permissions(self.instance,
                                                  self._model_name)
        except ObjectDoesNotExist:
            raise AuthorizeException(trans(
                "Cannot retrieve permissions for this object because "
                "it does not have an instance associated with it."))

        if direct_only:
            perm_set = {perm.field_name
                        for perm in perms
                        if perm.permission_level ==
                        FieldPermission.WRITE_DIRECTLY}
        else:
            perm_set = {perm.field_name for perm in perms
                        if perm.allows_writes}
        return perm_set

    def user_can_delete(self, user):
        """
        A user is able to delete an object if they have all
        field permissions on a model.
        """

        is_admin = user.get_role(self.instance).name == 'administrator'
        if is_admin:
            return True
        else:
            #TODO: This isn't checking for UDFs... should it?
            return self._get_perms_set(user) >= set(self.tracked_fields)

    def user_can_create(self, user, direct_only=False):
        """
        A user is able to create an object if they have permission on
        all required fields of its model.

        If direct_only is False this method will return true
        if the user has either permission to create directly or
        create with audits
        """
        can_create = True

        perm_set = self._get_perms_set(user, direct_only)

        for field in self._fields_required_for_create():
            if field.name not in perm_set:
                can_create = False
                break

        return can_create

    def _assert_not_masked(self):
        """
        Raises an exception if the object has been masked.
        This assertion should be called by any method that
        shouldn't operate on masked models.
        """
        if self._has_been_masked:
            raise AuthorizeException(
                "Operation cannot be performed on a masked object.")

    def get_pending_fields(self, user):
        """
        Evaluates the permissions for the current user and collects
        fields that inheriting subclasses will want to treat as
        special pending_edit fields.
        """
        perms = user.get_instance_permissions(self.instance, self._model_name)
        fields_to_audit = []
        for perm in perms:
            if ((perm.permission_level == FieldPermission.WRITE_WITH_AUDIT and
                 perm.field_name in self._updated_fields())):

                fields_to_audit.append(perm.field_name)

        return fields_to_audit

    def mask_unauthorized_fields(self, user):
        perms = user.get_instance_permissions(self.instance, self._model_name)
        readable_fields = {perm.field_name for perm
                           in perms
                           if perm.allows_reads}

        fields = set(self._previous_state.keys())
        unreadable_fields = fields - readable_fields

        for field_name in unreadable_fields:
            self.apply_change(field_name, None)

        self._has_been_masked = True

    def _perms_for_user(self, user):
        if user is None or user.is_anonymous():
            perms = self.instance.default_role.fieldpermission_set
        else:
            perms = user.get_instance_permissions(
                self.instance, self._model_name)

        return perms.filter(model_name=self._model_name)

    def visible_fields(self, user):
        perms = self._perms_for_user(user)
        return [perm.field_name for perm in perms if perm.allows_reads]

    def field_is_visible(self, user, field):
        return field in self.visible_fields(user)

    def editable_fields(self, user):
        perms = self._perms_for_user(user)
        return [perm.field_name for perm in perms if perm.allows_writes]

    def field_is_editable(self, user, field):
        return field in self.editable_fields(user)

    @staticmethod
    def mask_queryset(qs, user):
        for model in qs:
            model.mask_unauthorized_fields(user)
        return qs

    def save_with_user(self, user, *args, **kwargs):
        self._assert_not_masked()

        if self.pk is not None:
            writable_perms = self._get_perms_set(user)
            for field in self._updated_fields():
                if field not in writable_perms:
                    raise AuthorizeException("Can't edit field %s on %s" %
                                            (field, self._model_name))

        elif not self.user_can_create(user):
            raise AuthorizeException("%s does not have permission to "
                                     "create new %s objects." %
                                     (user, self._model_name))

        super(Authorizable, self).save_with_user(user, *args, **kwargs)

    def save_with_user_without_verifying_authorization(self, user,
                                                       *args, **kwargs):

        if isinstance(self, Auditable):
            kwargs.update({'unsafe': True})

        super(Authorizable, self).save_with_user(user, *args, **kwargs)

    def delete_with_user(self, user, *args, **kwargs):
        self._assert_not_masked()

        if self.user_can_delete(user):
            super(Authorizable, self).delete_with_user(user, *args, **kwargs)
        else:
            raise AuthorizeException("%s does not have permission to "
                                     "delete %s objects." %
                                     (user, self._model_name))


class AuditException(Exception):
    pass


class Auditable(UserTrackable):
    """
    Watches an object for changes and logs them

    You probably want to inherit this mixin after
    Authorizable, and not before.

    Ex.
    class Foo(Authorizable, Auditable, models.Model):
        ...
    """
    def __init__(self, *args, **kwargs):
        super(Auditable, self).__init__(*args, **kwargs)
        self.is_pending_insert = False

    def full_clean(self, *args, **kwargs):
        if not isinstance(self, Authorizable):
            super(Auditable, self).full_clean(*args, **kwargs)
        else:
            raise TypeError("all calls to full clean must be done via "
                            "'full_clean_with_user'")

    def full_clean_with_user(self, user):
        if ((not isinstance(self, Authorizable) or
             self.user_can_create(user, direct_only=True))):
            exclude_fields = []
        else:
            # If we aren't making a real object then we shouldn't
            # check foreign key contraints. These will be checked
            # when the object is actually made. They are also enforced
            # a the database level
            exclude_fields = []
            for field in self._fields_required_for_create():
                if isinstance(field, models.ForeignKey):
                    exclude_fields.append(field.name)

        super(Auditable, self).full_clean(exclude=exclude_fields)

    def audits(self):
        return Audit.audits_for_object(self)

    def delete_with_user(self, user, *args, **kwargs):
        a = Audit(
            model=self._model_name,
            model_id=self.pk,
            instance=self.instance if hasattr(self, 'instance') else None,
            user=user, action=Audit.Type.Delete)

        super(Auditable, self).delete_with_user(user, *args, **kwargs)
        a.save()

    def get_active_pending_audits(self):
        return self.audits()\
                   .filter(requires_auth=True)\
                   .filter(ref__isnull=True)\
                   .order_by('-created')

    def validate_foreign_keys_exist(self):
        """
        This method walks each field in the
        model to see if any foreign keys are available.

        There are cases where an object has a reference
        to a pending foreign key that is not caught during
        the django field validation process. Running this
        validation protects against these.
        """
        for field in self._meta.fields:

            is_fk = isinstance(field, models.ForeignKey)
            is_required = (field.null is False or field.blank is False)

            if is_fk and field != self._meta.pk:
                try:
                    related_model = getattr(self, field.name)
                    if related_model is not None:
                        id = related_model.pk
                        cls = field.rel.to
                        cls.objects.get(pk=id)
                    elif is_required:
                        raise IntegrityError(
                            "%s has null required field %s" %
                            (self, field.name))
                except ObjectDoesNotExist:
                    raise IntegrityError("%s has non-existent %s" %
                                         (self, field.name))

    def save_with_user(self, user, *args, **kwargs):

        # A concession. This is a direct message from the Authorizable class
        # to the Auditable class, even though ideally they shouldn't need to
        # know about each other. Even though this method is inside Auditable
        # some Auth tasks are performed in this method, coupling the two
        # classes. Since that is happening, the Auth class needs to be able to
        # communicate in order to bypass the normal flow of authorization.
        # This conditional block receives the 'unsafe' directive from the
        # Authorizable class and then removes it from the kwargs, preserving
        # the normal save_with_user API.
        # TODO: decouple Authorizable from Auditable.
        if kwargs.get('unsafe', None):
            auth_bypass = True
            kwargs = {k: v for k, v in kwargs.items()
                      if k != 'unsafe'}
        else:
            auth_bypass = False

        if self.is_pending_insert:
            raise Exception("You have already saved this object.")

        updates = self._updated_fields()

        is_insert = self.pk is None
        action = Audit.Type.Insert if is_insert else Audit.Type.Update

        pending_audits = []
        pending_fields = self.get_pending_fields(user)

        for pending_field in pending_fields:

            pending_audits.append((pending_field, updates[pending_field]))

            # Clear changes to object
            oldval = updates[pending_field][0]
            try:
                self.apply_change(pending_field, oldval)
            except ValueError:
                pass

            # If a field is a "pending field" then it should
            # be logically removed from the fields that are being
            # marked as "updated"
            del updates[pending_field]

        instance = self.instance if hasattr(self, 'instance') else None

        if ((not isinstance(self, Authorizable) or
             self.user_can_create(user, direct_only=True) or
             self.pk is not None or
             auth_bypass)):
            super(Auditable, self).save_with_user(user, *args, **kwargs)
            model_id = self.pk
        else:
            model_id = _reserve_model_id(
                _get_authorizable_class(self._model_name))
            self.pk = model_id
            self.id = model_id  # for e.g. Plot, where pk != id
            self.is_pending_insert = True

        if is_insert:
            if self.is_pending_insert:
                pending_audits.append(('id', (None, model_id)))
            else:
                updates['id'] = [None, model_id]

        def make_audit_and_save(field, prev_val, cur_val, pending):

            Audit(model=self._model_name, model_id=model_id,
                  instance=instance, field=field,
                  previous_value=prev_val,
                  current_value=cur_val,
                  user=user, action=action,
                  requires_auth=pending,
                  ref=None).save()

        for [field, values] in updates.iteritems():
            make_audit_and_save(field, values[0], values[1], False)

        for (field, (prev_val, next_val)) in pending_audits:
            make_audit_and_save(field, prev_val, next_val, True)

    @property
    def hash(self):
        """ Return a unique hash for this object """
        # Since this is an audited object each change will
        # manifest itself in the audit log, essentially keeping
        # a revision id of this instance. Since each primary
        # key will be unique, we can just use that for the hash
        audits = Audit.objects.filter(instance__pk=self.instance_id)\
                              .filter(model=self._model_name)\
                              .filter(model_id=self.pk)\
                              .order_by('-updated')

        # Occasionally Auditable objects will have no audit records,
        # this can happen if it was imported without using save_with_user
        try:
            audit_string = str(audits[0].pk)
        except IndexError:
            audit_string = 'none'

        string_to_hash = '%s:%s:%s' % (self._model_name, self.pk, audit_string)

        return hashlib.md5(string_to_hash).hexdigest()

    @classmethod
    def action_format_string_for_audit(clz, audit):
        if audit.field == 'id' or audit.field is None:
            lang = {
                Audit.Type.Insert: trans('created a %(model)s'),
                Audit.Type.Update: trans('updated the %(model)s'),
                Audit.Type.Delete: trans('deleted the %(model)s'),
                Audit.Type.PendingApprove: trans('approved an '
                                                 'edit to the %(model)s'),
                Audit.Type.PendingReject: trans('rejected an '
                                                'edit to the %(model)s')
            }
        else:
            lang = {
                Audit.Type.Insert: trans('set %(field)s to %(value)s'),
                Audit.Type.Update: trans('set %(field)s to %(value)s'),
                Audit.Type.Delete: trans('deleted %(field)s'),
                Audit.Type.PendingApprove: trans('approved setting '
                                                 '%(field)s to %(value)s'),
                Audit.Type.PendingReject: trans('rejecting setting '
                                                '%(field)s to %(value)s')
            }
        return lang[audit.action]


###
# TODO:
# Test fail in saving on base object
# Test null values
###
class Audit(models.Model):
    model = models.CharField(max_length=255, null=True, db_index=True)
    model_id = models.IntegerField(null=True, db_index=True)
    instance = models.ForeignKey(
        'Instance', null=True, blank=True, db_index=True)

    field = models.CharField(max_length=255, null=True)
    previous_value = models.TextField(null=True)
    current_value = models.TextField(null=True, db_index=True)

    user = models.ForeignKey('treemap.User')
    action = models.IntegerField()

    """
    These two fields are part of the pending edit system

    If requires_auth is True then this audit record represents
    a change that was *requested* but not applied.

    When an authorized user approves a pending edit
    it creates an audit record on this model with an action
    type of either "PendingApprove" or "PendingReject"

    ref can be set on *any* audit to note that it has been looked
    at and approved or rejected. If this is the case, the ref
    audit will be of type "ReviewApproved" or "ReviewRejected"

    An audit that is "PendingApproved/Rejected" cannot be be
    "ReviewApproved/Rejected"
    """
    requires_auth = models.BooleanField(default=False)
    ref = models.ForeignKey('Audit', null=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)

    class Type:
        Insert = 1
        Delete = 2
        Update = 3
        PendingApprove = 4
        PendingReject = 5
        ReviewApprove = 6
        ReviewReject = 7

    TYPES = {
        Type.Insert: trans('Create'),
        Type.Delete: trans('Delete'),
        Type.Update: trans('Update'),
        Type.PendingApprove: trans('Approved Pending Edit'),
        Type.PendingReject: trans('Reject Pending Edit'),
        Type.ReviewReject: trans('Rejected Edit'),
        Type.ReviewApprove: trans('Approved Edit')
    }

    def _deserialize_value(self, value):
        """
        A helper method to transform deserialized audit strings

        When an audit record is written to the audit table, the
        value is stored as a string. When deserializing these values
        for presentation purposes or constructing objects, they
        need to be converted to their correct python value.

        Where possible, django model field classes are used to
        convert the value.
        """

        # some django fields can't handle .to_python(None), but
        # for insert audits (None -> <value>) this method will
        # still be called.
        if value is None:
            return None

        # get the model/field class for each audit record and convert
        # the value to a python object
        cls = _get_auditable_class(self.model)
        field_query = cls._meta.get_field_by_name(self.field)
        field_cls, fk_model_cls, is_local, m2m = field_query
        field_modified_value = field_cls.to_python(value)

        # handle edge cases
        if isinstance(field_cls, models.GeometryField):
            field_modified_value = GEOSGeometry(field_modified_value)
        elif isinstance(field_cls, models.ForeignKey):
            field_modified_value = field_cls.rel.to.objects.get(
                pk=field_modified_value)

        return field_modified_value

    def _unit_format(self, value):
        model_name = self.model.lower()

        if isinstance(value, GEOSGeometry):
            if value.geom_type == 'Point':
                return '%d,%d' % (value.x, value.y)
            if value.geom_type in {'MultiPolygon', 'Polygon'}:
                value = value.area

        if is_convertible_or_formattable(model_name, self.field):
            _, value = get_display_value(
                self.instance, model_name, self.field, value)
            if value and is_convertible(model_name, self.field):
                units = get_unit_name(get_units(self.instance,
                                                model_name, self.field))
                value += (' %s' % units)

        return value

    @property
    def clean_current_value(self):
        if self.field and self.field.startswith('udf:'):
            return self.current_value
        else:
            return self._deserialize_value(self.current_value)

    @property
    def clean_previous_value(self):
        if self.field.startswith('udf:'):
            return self.previous_value
        else:
            return self._deserialize_value(self.previous_value)

    @property
    def current_display_value(self):
        return self._unit_format(self.clean_current_value)

    @property
    def previous_display_value(self):
        return self._unit_format(self.clean_previous_value)

    @property
    def field_display_name(self):
        if not self.field:
            return ''
        name = self.field
        if name.startswith('udf:'):
            return name[4:]
        else:
            return name.replace('_', ' ')

    @property
    def display_action(self):
        return Audit.TYPES[self.action]

    @classmethod
    def audits_for_model(clz, model_name, inst, pk):
        return Audit.objects.filter(model=model_name,
                                    model_id=pk,
                                    instance=inst).order_by('created')

    @classmethod
    def pending_audits(clz):
        return Audit.objects.filter(requires_auth=True)\
                            .filter(ref__isnull=True)\
                            .order_by('created')

    @classmethod
    def audits_for_object(clz, obj):
        return clz.audits_for_model(
            obj._model_name, obj.instance, obj.pk)

    def short_descr(self):
        cls = _get_auditable_class(self.model)
        # If a model has a defined short_descr method, use that
        if hasattr(cls, 'short_descr'):
            return cls.short_descr(self)

        format_string = cls.action_format_string_for_audit(self)

        return format_string % {'field': self.field_display_name,
                                'model': trans(self.model).lower(),
                                'value': self.current_display_value}

    def dict(self):
        return {'model': self.model,
                'model_id': self.model_id,
                'instance_id': self.instance.pk,
                'field': self.field,
                'previous_value': self.previous_value,
                'current_value': self.current_value,
                'user_id': self.user.pk,
                'action': self.action,
                'requires_auth': self.requires_auth,
                'ref': self.ref.pk if self.ref else None,
                'created': str(self.created)}

    def __unicode__(self):
        return u"pk=%s - action=%s - %s.%s:(%s) - %s => %s" % \
            (self.pk, self.TYPES[self.action], self.model,
             self.field, self.model_id,
             self.previous_value, self.current_value)

    def is_pending(self):
        return self.requires_auth and not self.ref


class ReputationMetric(models.Model):
    """
    Assign integer scores for each model that determine
    how many reputation points are awarded/deducted for an
    approved/denied audit.
    """
    instance = models.ForeignKey('Instance')
    model_name = models.CharField(max_length=255)
    action = models.CharField(max_length=255)
    direct_write_score = models.IntegerField(null=True, blank=True)
    approval_score = models.IntegerField(null=True, blank=True)
    denial_score = models.IntegerField(null=True, blank=True)

    def __unicode__(self):
        return "%s - %s - %s" % (self.instance, self.model_name, self.action)

    @staticmethod
    def apply_adjustment(audit):
        try:
            rm = ReputationMetric.objects.get(instance=audit.instance,
                                              model_name=audit.model,
                                              action=audit.action)
        except ObjectDoesNotExist:
            return

        iuser = audit.user.get_instance_user(audit.instance)

        if audit.requires_auth and audit.ref:
            review_audit = audit.ref
            if review_audit.action == Audit.Type.PendingApprove:
                iuser.reputation += rm.approval_score
                iuser.save_base()
            elif review_audit.action == Audit.Type.PendingReject:
                new_score = iuser.reputation - rm.denial_score
                if new_score >= 0:
                    iuser.reputation = new_score
                else:
                    iuser.reputation = 0
                iuser.save_base()
            else:
                error_message = ("Referenced Audits must carry approval "
                                 "actions. They must have an action of "
                                 "PendingApprove or Pending Reject. "
                                 "Something might be very wrong with your "
                                 "database configuration.")
                raise IntegrityError(error_message)
        elif not audit.requires_auth:
            iuser.reputation += rm.direct_write_score
            iuser.save_base()


@receiver(post_save, sender=Audit)
def audit_presave_actions(sender, instance, **kwargs):
    ReputationMetric.apply_adjustment(instance)


def _get_model_class(class_dict, cls, model_name):
    """
    Convert a model name (as a string) into the model class
    """
    if model_name.startswith('udf:'):
        from udf import UserDefinedCollectionValue
        return UserDefinedCollectionValue

    if not class_dict:
        # One-time load of class dictionary
        for c in leaf_subclasses(cls):
            class_dict[c.__name__] = c

    return class_dict[model_name]


_auditable_classes = {}
_authorizable_classes = {}

_get_auditable_class = partial(_get_model_class, _auditable_classes, Auditable)
_get_authorizable_class = partial(_get_model_class, _authorizable_classes,
                                  Authorizable)
