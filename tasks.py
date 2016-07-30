#!/usr/bin/env python
from __future__ import absolute_import
import ast
import sys
import csv
import os

from celery import Celery, current_task, group, chord, chain
from functools import wraps
import celery.registry as registry

from app import db
import app.models
import config
import extract

celery = Celery('tasks',
                broker="sqla+"+config.SQLALCHEMY_DATABASE_URI,
                backend="db+"+config.SQLALCHEMY_DATABASE_URI)

# Get rid of pickle (insecure, causes long deprecation warning at startup)
celery.conf.CELERY_TASK_SERIALIZER = 'json'
celery.conf.CELERY_ACCEPT_CONTENT = ['json', 'msgpack', 'yaml']


# Need to figure out some details here.  Currently, this file uses the
# SQLAlchemy object from Flask to access db.  That's probably wrong.
# See:
# http://prschmid.blogspot.com/2013/04/using-sqlalchemy-with-celery-tasks.html

class SqlAlchemyTask(celery.Task):
    """An abstract Celery Task that ensures that the connection the the
    database is closed on task completion"""
    abstract = True
    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        db.session.remove()

# Decorator to turn a task into a task that tries to retry
# itself. It's a bit ugly, but we often want to run some asynchronous
# task on an on an object that we've only just created (but have not
# committed).  By setting up tasks to retry, we'll eventually get the
# work done, generally on the first retry, since it will run after the
# commit.
def that_retries(task):
    @wraps(task)
    def retrying(*args, **kwargs):
        try:
            task(*args, **kwargs)
        except Exception as e:
            current_task.retry(exc = e, countdown=30)
    return retrying


# Celery won't let you chain groups. If you do, the first group
# becomes a chord, trying to feed its result into the second group
# - which doesn't work.
    
# So, we make the first group into a chord that feeds a dummy task.
# The chord can be can used as the first member of a chain.

@celery.task
def done(*args, **kwargs):
    '''A no-op task, used to work around the chord/group/chain issue'''
    return "DONE"


def if_dataset(ds):
    if ds:
        dataset.delay(ds.id)

@celery.task
def dataset(ds_id):
    ds = app.models.Dataset.query.get(ds_id)
    for blob in ds.blobs:
        # analyze_blob(ds.id, blob.id)
        analyze_blob.delay(ds.id, blob.id)
    for kw in ds.keywords:
        add_examples(kw)

@celery.task(base=SqlAlchemyTask)
def analyze_blob(ds_id, blob_id):
    ds = app.models.Dataset.query.get(ds_id)
    blob = app.models.Blob.query.get(blob_id)
    ds.create_blob_features(blob)


def add_examples(k):
    # read definition file
    with open(k.defn_file) as defn:
        for ex_ind, row in enumerate(csv.reader(defn)):
            # create examples for each row
            blob_name, x, y, w, h, val = row
            x, y, w, h = int(x), int(y), int(w), int(h)

            # check if blob exists
            blobs = k.dataset.blobs
            if not blobs:
                print 'Cannot add example from empty dataset {}'.format(k.dataset)
                return
            print blobs    
            blobs = [b for b in blobs
                     if blob_name in b.location]
            
            if not blobs:
                # TODO: add log entry
                print 'Cannot add example for file {}'.format(blob_name)
                return


            blob = blobs[0]
            patch = app.models.Patch.ensure(blob=blob, x=x, y=y, height=h, width=w,
                                            fliplr=False, rotation=0.0)
            # Calculate features for the example patches (as needed)
            for fs in k.dataset.featurespecs:
                print patch
                print patch.image.shape
                feat = fs.analyze_patch(patch)
                if feat:
                    db.session.add(feat)
                # TODO put this counting and del_networks() inside CNN
                if fs.instance.__class__ is extract.CNN and (ex_ind > 0 and ex_ind % 1000 == 0):
                    fs.instance.del_networks()
            ex = app.models.Example(value=val, patch=patch, keyword=k)
            db.session.add(ex)
        db.session.commit()


@celery.task
def keyword(kw_id):
    kw = app.models.Keyword.query.get(kw_id)
    for seed in kw.seeds:
        seed.patch.materialize()

def if_classifier(c):
    if c:
        classifier.delay(c.id)

@celery.task
def classifier(c_id):
    c = app.models.Classifier.query.get(c_id)
    kw = c.keyword
    ds = c.dataset

    # Start the classifier with seeds from the keyword
    negative = False;
    zero = c.rounds[0]
    for ex in kw.seeds:
        e = app.models.Example(value = ex.value, patch = ex.patch, round = zero)
        db.session.add(e)

        # We added at least one negative value from the seeds
        if not ex.value:
            negative = True

        # Calculate features for the example patches (as needed)
        for fs in ds.featurespecs:
            feat = fs.analyze_patch(ex.patch)
            if feat:
                db.session.add(feat)

    # If no negative seeds, cross fingers and add one "random" patch
    # to serve as negative. It will already have the features
    # calculated, since it comes from the dataset.

    # It would be preferable to only do this when the Estimator in use
    # really needs negative examples to work well (or add interface to
    # accept negatives, and require them in such cases).
    
    if not negative:
        patch = ds.blobs[0].patches[0]
        e = app.models.Example(value = False, patch = patch, round = zero)
        db.session.add(e)

    predict_round(zero.id)
    db.session.commit()

@celery.task
def advance_classifier(c_id):
    classifier = app.models.Classifier.query.get(c_id)
    latest_round = classifier.latest_round
    round = app.models.Round(classifier = classifier,
                             number = latest_round.number+1)
    db.session.add(round)

    for pq in latest_round.queries:
        value = pq.responses[0].value # should be a vote, avg, etc
        ex = app.models.Example(value=value, patch=pq.patch, round=round)
        db.session.add(ex)

    predict_round(round.id)
    db.session.commit();


def predict_round(r_id):
    round = app.models.Round.query.get(r_id)

    for pred in round.predict():
        db.session.add(pred)

    for pq in round.choose_queries():
        db.session.add(pq)

    db.session.commit()
    precrop_round_results.delay(r_id)


@celery.task
def precrop_round_results(r_id):
    round = app.models.Round.query.get(r_id)
    for pq in round.queries:
        pq.patch.materialize()

@celery.task
def detect(d_id):
    detect = app.models.Detection.query.get(d_id)
    dense = app.models.PatchSpec.query.filter_by(name='Dense').one()
    # Patch the blob
    for patch in dense.create_blob_patches(detect.blob):
        db.session.add(patch)
    for c in app.models.Classifier.query.all():
        print c
        # Create features for the patches
        for fs in c.dataset.featurespecs:
            print " ",fs
            for f in fs.analyze_blob(detect.blob):
                print "  ",f
                db.session.add(f)
        # Test the patches of the blob, saving Predictions
        for p in c.latest_round.detect(detect.blob):
            print " ",p
            db.session.add(p)
    db.session.commit()

if __name__ == "__main__":
    function = sys.argv[1]
    ids = [int(s) for s in sys.argv[2:]]
    print function, ids
    task = registry.tasks["tasks."+function]
    task(*ids)
    
