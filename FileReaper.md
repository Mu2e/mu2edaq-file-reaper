# Mu2edaq File Reaper

This is an application that cleans up specific areas of the mu2edaq data disks and other storage areas on the online systems.  The application should:

1. Monitor the available diskspace on different areas of the Mu2e DAQ system.
2. When available free space crosses different configurable thresholds, the system should send alarm messages to the other Mu2e DAQ systems.  Then it should analyze the files in the given area and perform different actions to clean up the disk area by moving or deleting files.

# General Design

Base the general web elements of this application after the mu2edaq-diskwatcher application.  Assume a similar design and configuration system.  The difference is that this application will specifically be allowed to modify the contents of the file system.

# Policy based deletion

The reaper application will delete files when the available free space goes below a configured threshold.  There are three thresholds, warning, critical, and full.  Each has a different delete policy.  Each can be configured differently.

## Low and High Water Marks

For each threshold, there are low and high water marks related to that threshold.  The low water mark is specified as a percentage below the threshold.  The default low water mark is 10% below the threshold.  The high water mark is specified as a percentage above threshold, with a default of 0 so that it is the same as the threshold value.


## Deletion policies

* The first deletion policy is a "least recently used" policy combined with a minimum age of the file.  The name of this policy is "LRU-Delete".  Under this policy the files are ordered by last access time and last modification time using which ever is the the most recent as the comparision metric.  If a file is older than the specified age threshold then it is eligible for deletion.  Files that are eligible for deletion are deleted in the order of their last access time, oldest access time first, until the available diskspace reaches a configured "low watermark" threshold.  If this policy does not result in the free space dropping below the configured threshold, then a warning is issued to the DAQ system.

* The second deletion policy is a strict file age policy. The name of this policy is "Age-Delete"  Any file that is older than a specified age threshold is eligible for deletion.  Files are deleted in order from the oldest to newest until the free space on the disk reaches the configured low water mark for this policy.  If this policy does not result in the free space dropping below the configured threshold, then a warning is issued to the DAQ system.

* The third policy is a "least recently used" policy combined with a minimum age of the file, but instead of deleting the files, the files are compressed by a standard compression routine. The name of this policy is "LRU-Compress" If this policy does not result in the free space dropping below the configured threshold, then a warning is issued to the DAQ system.

* The forth policy is a strict file age policy but with a compression instead of deletion.  The name of this policy is "Age-Compress".  Any file that is older than a specified age threshold is eligible for compression.  Files are compressed in order from the oldest to newest until the free space on the disk reaches the configured low water mark for this policy.  If this policy does not result in the free space dropping below the configured threshold, then a warning is issued to the DAQ system.

## Scan behavior

The application scans the configured disk areas on a configurable time delay.  By default the delay should be 10 minutes.

Scans should be asynchronous from other activity.

Scans should:
* Determine which files are eligible for deletion or compression in each configured area.
* Sort the files that are eligible for deletion or compression to create deletion and compression queues which are ordered.
* Publish their queues to the application interface
* Take action on the compression queues
* Take action on the deletion queues

## History and Accounting

The application needs to keep an audit history of actions that it has taken.  It should include:

* Date/Time of when a file is added to a queue and the conditions that triggered it
* Date/Time of when an action is taken on a file (i.e. delete or compress)
* Date/Time of when a file is removed from a queue and the action that caused the removal
* Date/Time of when a file is marked as excluded
* Other actions taken on files or queues

The application should support querying the history from the web interface and from the API.

## Notification

The application should be able to send notifications on the actions that it has taken.

Notifications should be able to be sent via:

* Mu2e notification system
* Mu2e daq messages
* Email
* Slack

Each notification channel should be able to be configured independantly.  

## Compression and Deletion Queues

* The queues for compression and deletion should be viewable from the application interface.
* Details on entries in the queue should be viewable.  The details should include file size, last access and modification times, owner and complete path, and status (eligible for deletion, eligible for compression)
* And entry should be able to be marked as "exclude" to exclude it from deletion or compression

# Discovery

The application should publish it's label and port assignments via the mu2e discovery system.  It should publish the main web interface ports and the API ports.  Because there will be more than one reaper running across the computing cluster, there need to be labels on each instance.

# Interface

* The interface should support disabling the deletion policy on a disk path or volume.
* The interface should support pausing and re-enabling the deletion routines for a disk path or volume.

# API

There should be an API interface to the system.  It should expose all of the functions.
The API should use a bearer token model.
Each token should be able to be registered and managed through the web interface.

# Commandline tools

There should be commandline tools for interacting with the application.  These tools should go through the API interface.  The commandline tools should be able to cache its tokens.