#!/usr/bin/env python
##########################################################################################
#
# This script does a rollout of an autoscaling group gradually, while waiting/checking
# that whatever elasitc load balancer or target group it is attached to is healthy before
# continuing (if attached)
#
# This script leverages boto and the python aws scripts helper.  There is no option to
# set the AWS region or credentials in this script, but boto reads from typical AWS
# environment variables/profiles so to set the region, please use the typical aws-cli
# methods to do so, same goes for the AWS credentials.  Eg:
#
#   AWS_DEFAULT_PROFILE=client_name AWS_DEFAULT_REGION=us-east-1 aws-autoscaling-rollout.py -a autoscalername
#
# WARNING: This script does NOT work (yet) for doing rollouts of autoscaled groups that are
#          attached to ALBs that are used in an ECS cluster.  That's a WHOLE other beast,
#          that I would love for this script to handle one day... but alas, it does not yet.
#          If you try to use this script against an autoscaler that is used in an ECS cluster
#          it will have unexpected and most likely undesired results.  So be warned!!!!!!!
#
# The latest version of this code and more documentation can be found at:
#       https://github.com/DevOps-Nirvana/aws-missing-tools
#
# Author:
#       Farley <farley@neonsurge.com> <farley@olindata.com>
#
##########################################################################################

######################
# Libraries and instantiations of libraries
######################
import boto3
import time
import os
import logging
# For CLI Parsing of args
from optparse import OptionParser
# This is for the pre/post external health check feature
from subprocess import call
try:
    elb = boto3.client('elb')
    autoscaling = boto3.client('autoscaling')
    ec2 = boto3.client('ec2')
    elbv2 = boto3.client('elbv2')
except:
    elb = boto3.client('elb', region_name='eu-west-1')
    autoscaling = boto3.client('autoscaling', region_name='eu-west-1')
    ec2 = boto3.client('ec2', region_name='eu-west-1')
    elbv2 = boto3.client('elbv2', region_name='eu-west-1')


LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(level=LOG_LEVEL, format='%(levelname)s: %(asctime)s: %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

######################
# CLI Argument handling
######################
usage = "usage: %prog -a autoscaler"
parser = OptionParser(usage=usage)
parser.add_option("-a", "--autoscaler",
                  dest="autoscaler",
                  default="",
                  help="Autoscaler to rollout",
                  metavar="as-name")
parser.add_option("-f", "--force",
                  dest="force",
                  action="store_true",
                  help="If we want to force-deployment by skipping health pre-checks, and will ignore and reset currently suspended processes.  NOTE: This will NOT skip the external health check commands or wait for seconds")
parser.add_option("-s", "--skip-elb-health-check",
                  dest="skip",
                  action="store_true",
                  help="If we want to skip the ELB health check of new instances as they come up (often used with --force)")
parser.add_option("-w", "--wait-for-seconds",
                  dest="waitforseconds",
                  default="0",
                  type="int",
                  help="The number of extra seconds to wait in-between instance terminations (0 to disable)",
                  metavar="seconds")
parser.add_option("-u", "--check-if-new-server-is-up-command",
                  dest="checkifnewserverisupcommand",
                  default="",
                  help="An external health check command to run to verify a new instance is healthy before continuing deployment.  This should be a valid 'shell' command that can run on this server.  This command supports _simple_ templating in the form of string replacing NEW_INSTANCE_ID, NEW_INSTANCE_PRIVATE_IP_ADDRESS, NEW_INSTANCE_PUBLIC_IP_ADDRESS.  Often used to do custom health checks when an autoscaler is not attached to an ELB.  This feature could also be used to add ECS support with a little creativity.  When this command returns retval of 0 then the deployment continues",
                  metavar="command")
parser.add_option("-b", "--run-before-server-going-down-command",
                  dest="runbeforeserverdowncommand",
                  default="",
                  help="An external command to run before a server goes down, this is run BEFORE the wait-for-seconds.  This should be a valid 'shell' command that can run on this server.  This command supports _simple_ templating in the form of string replacing OLD_INSTANCE_ID, OLD_INSTANCE_PRIVATE_IP_ADDRESS, OLD_INSTANCE_PUBLIC_IP_ADDRESS.  Often used to do stuff like pull a server out of a cluster (eg: to force-leave Consul).  This feature could also be used to add ECS support with a little creativity.  This command MUST return a retval of 0 otherwise this deployment will halt.",
                  metavar="command")
parser.add_option("-d", "--run-after-server-going-down-command",
                  dest="runafterserverdowncommand",
                  default="",
                  help="An external command to run before a server goes down, this is run BEFORE the wait-for-seconds.  This should be a valid 'shell' command that can run on this server.  This command supports _simple_ templating in the form of string replacing OLD_INSTANCE_ID, OLD_INSTANCE_PRIVATE_IP_ADDRESS, OLD_INSTANCE_PUBLIC_IP_ADDRESS.  Often used to do stuff like pull a server out of a cluster (eg: to force-leave Consul).  This command MUST return a retval of 0 otherwise this deployment will halt.",
                  metavar="command")
parser.add_option("-c", "--check-if-instances-need-to-be-terminated",
                  dest="checkifinstancesneedtobeterminated",
                  action="store_true",
                  help="Check if instance launch configuration or launch template is already updated.  This is useful in case the rollout fails and leave an Auto Scaling Group with a lot of instances partially updated.")
(options, args) = parser.parse_args()

# Startup simple checks...
if options.autoscaler == "":
    logger.info("ERROR: You MUST specify the autoscaler with -a")
    parser.print_usage()
    exit(1)
if options.force:
    logger.info("ALERT: We are force-deploying this autoscaler, which may cause downtime under some circumstances")
if options.skip:
    logger.info("ALERT: We are skipping ELB health checks of new instances as they come up, this will probably cause downtime")


######################
# Helper functions
######################


# Get a load balancer
def get_load_balancer(loadbalancer_name):
    try:
        fetched_data = elb.describe_load_balancers(
            LoadBalancerNames=[
                loadbalancer_name,
            ],
            PageSize=1
        )

        if len(fetched_data['LoadBalancerDescriptions']) > 0:
            return fetched_data['LoadBalancerDescriptions'][0]
    except Exception as e:
        raise Exception("Error searching for loadbalancer with name [{}]".format(loadbalancer_name), e)
    raise Exception("No loadbalancer found with name [{}]".format(loadbalancer_name))


# Get a application load balancer
def get_application_load_balancer( loadbalancer_name ):
    try:
        fetched_data = elbv2.describe_load_balancers(
            Names=[
                loadbalancer_name,
            ],
        )
        if len(fetched_data['LoadBalancers']) > 0:
            return fetched_data['LoadBalancers'][0]
    except Exception as e:
        raise Exception("Error searching for loadbalancer with name [{}]".format(loadbalancer_name), e)
    raise Exception("No loadbalancer found with name [{}]".format(loadbalancer_name))

# Describe launch configuration
def describe_launch_configuration( launch_configuration_name ):
    try:
        fetched_data = autoscaling.describe_launch_configurations(
            LaunchConfigurationNames=[
                launch_configuration_name,
            ],
        )
        if len(fetched_data['LaunchConfigurations']) > 0:
            return fetched_data['LaunchConfigurations'][0]
    except Exception as e:
        raise Exception("Error searching for launch configuration with name [{}]".format(launch_configuration_name), e)
    raise Exception("No launch configuration found with name [{}]".format(launch_configuration_name))

# Update auto scaling group max size
def update_auto_scaling_group_max_size( autoscaling_group_name, max_size ):
    response = autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=autoscaling_group_name,
        MaxSize=max_size
    )
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        return True
    else:
        logger.info("ERROR: Unable to set max autoscaling group size on '" + autoscaling_group_name + "'")
        return False

# Get target group
def get_target_group( target_group_name ):
    try:
        fetched_data = elbv2.describe_target_groups(
            Names=[
                target_group_name
            ],
            PageSize=1
        )
        if len(fetched_data['TargetGroups']) > 0:
            return fetched_data['TargetGroups'][0]
    except Exception as e:
        raise Exception("Error searching for target group with name [{}]".format(target_group_name), e)
    raise Exception("No target group found with name [{}]".format(target_group_name))


# Get a autoscaling group
def get_autoscaling_group( autoscaling_group_name ):
    try:
        fetched_data = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[
                autoscaling_group_name,
            ],
            MaxRecords=1
        )
        if len(fetched_data['AutoScalingGroups']) > 0:
            return fetched_data['AutoScalingGroups'][0]
    except Exception as e:
        raise Exception("Error searching for autoscaling group with name [{}]".format(autoscaling_group_name), e)
    raise Exception("Error searching for autoscaling group with name [{}]".format(autoscaling_group_name))


# Get all autoscaling groups
def get_all_autoscaling_groups( ):
    try:
        fetched_data = autoscaling.describe_auto_scaling_groups(
            MaxRecords=100
        )

        if AutoScalingGroups in fetched_data:
            return fetched_data['AutoScalingGroups']
    except Exception as e:
        raise Exception("Error getting all autoscaling groups", e)
    raise Exception("Error getting all autoscaling groups")


# Get autoscaling group configuration
def get_autoscaling_group_configuration(autoscaler):
    configuration = autoscaler.get('LaunchConfigurationName', None)
    if not configuration:
        configuration = autoscaler.get('MixedInstancesPolicy', None)
        if configuration:
            configuration = configuration['LaunchTemplate']['LaunchTemplateSpecification']['LaunchTemplateName']
        else:
            raise Exception(
                "Error searching configuration for autoscaling group with name [{}]".format(autoscaler['AutoScalingGroupName']))
    return configuration


# Get instance configuration
def get_instance_configuration(instance):
    configuration = instance.get('LaunchConfigurationName', None)
    if not configuration:
        configuration = instance.get('LaunchTemplate', None)
        if configuration:
            configuration = configuration['LaunchTemplateName']
        else:
            raise Exception(
                "Error searching configuration for instance with id [{}]".format(instance['InstanceId']))
    return configuration


# Return a list of instances to skip
def get_instances_to_skip(instances, autoscaler):
    output = []

    for instance in instances:
        if get_autoscaling_group_configuration(autoscaler) == get_instance_configuration(instance):
            output.append(instance)

    return output


# Gets the suspended processes for an autoscaling group (by name or predefined to save API calls)
def get_suspended_processes( autoscaling_group_name_or_definition ):
    if type(autoscaling_group_name_or_definition) is str:
        autoscaling_group = get_autoscaling_group( autoscaling_group_name_or_definition )
    else:
        autoscaling_group = autoscaling_group_name_or_definition

    output = []
    for item in autoscaling_group['SuspendedProcesses']:
        output.append(item['ProcessName'])

    return output

# Gets an single instance's details
def describe_instance(instance_id):
    # Get detailed instance information from the instances attached to the autoscaler
    instances = ec2.describe_instances(InstanceIds=[instance_id])
    for reservation in instances["Reservations"]:
        for instance in reservation["Instances"]:
            return instance


# Gets the suspended processes for an autoscaling group (by name or predefined to save API calls)
def suspend_processes( autoscaling_group_name, processes_to_suspend ):
    response = autoscaling.suspend_processes(
        AutoScalingGroupName=autoscaling_group_name,
        ScalingProcesses=processes_to_suspend
    )
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        return True
    else:
        logger.info("ERROR: Unable to suspend_processes on '" + autoscaling_group_name + "'")
        return False


# Gets the suspended processes for an autoscaling group (by name or predefined to save API calls)
def resume_processes( autoscaling_group_name, processes_to_resume ):
    response = autoscaling.resume_processes(
        AutoScalingGroupName=autoscaling_group_name,
        ScalingProcesses=processes_to_resume
    )
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        return True
    else:
        logger.info("ERROR: Unable to resume_processes on '" + autoscaling_group_name + "'")
        return False


def resume_all_processes( autoscaling_group_name ):
    response = autoscaling.resume_processes(
        AutoScalingGroupName=autoscaling_group_name
    )
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        return True
    else:
        logger.info("ERROR: Unable to resume_all_processes on '" + autoscaling_group_name + "'")
        return False


# Check if an autoscaler is currently performing a scaling activity
def check_if_autoscaler_is_scaling( autoscaling_group_name ):
    # Get the autoscaling group
    autoscaler = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[
            autoscaling_group_name,
        ],
        MaxRecords=1
    )

    # Quick error checking
    if len(autoscaler['AutoScalingGroups']) != 1:
        logger.info("ERROR: Unable to get describe autoscaling group: " + autoscaling_group_name)
        exit(1)
    autoscaler = autoscaler['AutoScalingGroups'][0]

    # Check if our healthy instance count matches our desired capacity
    healthy_instance_count = get_number_of_autoscaler_healthy_instances( autoscaler )
    if healthy_instance_count != autoscaler['DesiredCapacity']:
        logger.info("INFO: Our autoscaler must be scaling, desired " + str(autoscaler['DesiredCapacity']) + ", healthy instances " + str(healthy_instance_count))
        return True

    return False


def deregister_instance_from_load_balancer( instance_id, loadbalancer_name ):
    response = elb.deregister_instances_from_load_balancer(
        LoadBalancerName=loadbalancer_name,
        Instances=[
            {
                'InstanceId': instance_id
            },
        ]
    )
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        return True
    else:
        logger.info("ERROR: Unable to deregister instance '" + instance_id + "' from load balancer '" + loadbalancer_name + "'")
        return False


def deregister_instance_from_target_group( instance_id, target_group_arn ):
    response = elbv2.deregister_targets(
        TargetGroupArn=target_group_arn,
        Targets=[
            {
                'Id': instance_id,
            },
        ]
    )
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        return True
    else:
        logger.info("ERROR: Unable to deregister instance '" + instance_id + "' from load balancer '" + loadbalancer_name + "'")
        return False


def wait_for_autoscaler_to_have_healthy_desired_instances( autoscaling_group_name_or_definition ):
    if type(autoscaling_group_name_or_definition) is str:
        autoscaler_description = get_autoscaling_group( autoscaling_group_name_or_definition )
    else:
        autoscaler_description = autoscaling_group_name_or_definition
    # Get our desired capacity
    desired_capacity = int(autoscaler_description['DesiredCapacity'])

    while True:
        healthy_instance_count = int(get_number_of_autoscaler_healthy_instances( autoscaler_description['AutoScalingGroupName'] ))
        if desired_capacity != healthy_instance_count:
            logger.info("WARNING: We have " + str(healthy_instance_count) + " healthy instances on the autoscaler but we want " + str(desired_capacity))
        elif check_if_autoscaler_is_scaling( autoscaler_description['AutoScalingGroupName'] ):
            logger.info("WARNING: We are currently performing some autoscaling, we should wait...")
        else:
            logger.info("SUCCESS: We currently have desired capacity of " + str(desired_capacity) + " on this autoscaler")
            break
        logger.info("Waiting for 5 seconds...")
        time.sleep(5)


# Get the number of healthy instances from the autoscaling group definition
def get_number_of_autoscaler_healthy_instances( autoscaler_description ):
    return len(get_autoscaler_healthy_instances( autoscaler_description ))


# Get the healthy instances from the autoscaling group definition or name
def get_autoscaler_healthy_instances( autoscaling_group_name_or_definition ):
    if type(autoscaling_group_name_or_definition) is str:
        autoscaler_description = get_autoscaling_group( autoscaling_group_name_or_definition )
    else:
        autoscaler_description = autoscaling_group_name_or_definition

    healthy_instances = []
    for instance in autoscaler_description['Instances']:
        if instance['HealthStatus'] == 'Healthy':
            healthy_instances.append(instance)
    return healthy_instances


def terminate_instance_in_auto_scaling_group( instance_id, autoscaling_group_name, decrement_capacity=False ):
    logger.info("Terminating instance '" + instance_id + "' from the autoscaling group '" + autoscaling_group_name + "'...")

    if decrement_capacity is True:
        response = autoscaling.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=True
        )
    else:
        response = autoscaling.terminate_instance_in_auto_scaling_group(
            InstanceId=instance_id,
            ShouldDecrementDesiredCapacity=False
        )

    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        logger.info("Executed okay")
        return True
    else:
        logger.info("ERROR: Unable to detach autoscaler '" + autoscaling_group_name + "' from the load balancer '" + loadbalancer_name)
        exit(1)


def set_desired_capacity( autoscaling_group_name, desired_capacity ):
    logger.info("Setting desired capacity of '" + autoscaling_group_name + "' to '" + str(desired_capacity) + "'...")
    response = autoscaling.set_desired_capacity(
        AutoScalingGroupName=autoscaling_group_name,
        DesiredCapacity=desired_capacity,
        HonorCooldown=False
    )

    # Check if this executed okay...
    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        logger.info("Executed okay")
        return True
    else:
        logger.info("ERROR: Unable to set_desired_capacity on '" + autoscaling_group_name + "'")
        exit(1)


def get_instance_ids_of_target_group( target_group_arn ):

    response = elbv2.describe_target_health(
        TargetGroupArn=target_group_arn
    )

    output = []
    for target in response['TargetHealthDescriptions']:
        output.append(target['Target']['Id'])
    return output


def get_instance_ids_of_load_balancer( loadbalancer_name_or_definition ):
    if type(loadbalancer_name_or_definition) is str:
        loadbalancer = get_load_balancer( loadbalancer_name_or_definition )
    else:
        loadbalancer = loadbalancer_name_or_definition

    output = []
    for instance in loadbalancer['Instances']:
        output.append(instance['InstanceId'])
    return output


def wait_for_complete_targetgroup_autoscaler_attachment( target_group_arn, autoscaling_group_name ):

    logger.info("Waiting for attachment of autoscaler " + autoscaling_group_name + " to target_group_arn: " + target_group_arn)

    while True:
        # Get instances from target group
        logger.info("Getting target group instances")
        target_group = elbv2.describe_target_health(
            TargetGroupArn=target_group_arn
        )

        # Get healthy instance ids from target group
        logger.info("Getting instance ids from load balancer")
        instance_health_flat = []
        for instance in target_group['TargetHealthDescriptions']:
            if (instance['TargetHealth']['State'] == 'healthy'):
                instance_health_flat.append(instance['Target']['Id'])

        # Get our healthy instances from our autoscaler
        logger.info("Getting healthy instances on our autoscaler")
        autoscaler = get_autoscaling_group( autoscaling_group_name )
        as_instances = get_autoscaler_healthy_instances( autoscaler )

        successes = 0
        for instance in as_instances:
            if instance['InstanceId'] in instance_health_flat:
                logger.info("SUCCESS - Instance " + instance['InstanceId'] + " is healthy in our target group")
                successes = successes + 1
            else:
                logger.info("FAIL - Instance " + instance['InstanceId'] + " is unhealthy or not present in our target group")

        if successes >= len(as_instances):
            if int(autoscaler['DesiredCapacity']) == successes:
                logger.info("We have " + str(successes) + " healthy instances on the target group and on the ASG")
                break
            else:
                logger.info("FAIL - We have " + str(successes) + " healthy instances on the target group but we have desired instances set to " + str(autoscaler['DesiredCapacity']) + " on the ASG")
        else:
            logger.info("Found " + str(successes) + " healthy instances on the target group from the ASG " + str(autoscaler['DesiredCapacity']) + " to continue.  Waiting 10 seconds...")

        time.sleep( 10 )


def wait_for_instances_to_detach_from_loadbalancer( instance_ids, loadbalancer_name ):
    logger.info("Waiting for detachment of instance_ids ")
    logger.info(instance_ids)
    logger.info("   from load balancer:" + loadbalancer_name)

    while True:
        loadbalancer = get_load_balancer(loadbalancer_name)
        lb_instances = get_instance_ids_of_load_balancer(loadbalancer)

        failures = 0
        for instance in instance_ids:
            logger.info("  Checking if " + instance + " is attached to load balancer...")
            if instance in lb_instances:
                logger.info("    ERROR: Currently attached to the load balancer...")
                failures = failures + 1
            else:
                logger.info("    SUCCESS: Instance is not attached to the load balancer")

        if failures == 0:
            logger.info("SUCCESS: Done waiting for detachment of instance ids")
            break

        logger.info("Waiting for 10 seconds and trying again...")
        time.sleep( 10 )

    logger.info("DONE waiting for detachment of instances from " + loadbalancer_name)



def wait_for_instances_to_detach_from_target_group( instance_ids, target_group_arn ):
    logger.info("Waiting for detachment of instance_ids ")
    logger.info(instance_ids)
    logger.info("   from target group:" + target_group_arn)

    while True:
        logger.info("Getting target group instances")
        target_group = elbv2.describe_target_health(
            TargetGroupArn=target_group_arn
        )

        # Get healthy instance ids from target group
        logger.info("Getting instance ids from load balancer")
        instance_health_flat = []
        for instance in target_group['TargetHealthDescriptions']:
            instance_health_flat.append(instance['Target']['Id'])

        failures = 0
        for instance in instance_ids:
            logger.info("  Checking if " + instance + " is attached to target group...")
            if instance in instance_health_flat:
                logger.info("    ERROR: Currently attached to the target group...")
                failures = failures + 1
            else:
                logger.info("    SUCCESS: Instance is not attached to the target group")

        if failures == 0:
            logger.info("SUCCESS: Done waiting for detachment of instance ids")
            break

        logger.info("Waiting for 10 seconds and trying again...")
        time.sleep( 10 )

    logger.info("DONE waiting for detachment of instances from " + target_group_arn)



def wait_for_complete_targetgroup_autoscaler_detachment( target_group_arn, autoscaling_group_name ):

    logger.info("Waiting for detachment of autoscaler " + autoscaling_group_name + " from target_group_arn:" + target_group_arn)

    while True:
        # Get instances from target group
        logger.info("Getting target group instances")
        target_group = elbv2.describe_target_health(
            TargetGroupArn=target_group_arn
        )

        # Get healthy instance ids from target group
        logger.info("Getting instance ids from load balancer")
        instance_health_flat = []
        for instance in target_group['TargetHealthDescriptions']:
            instance_health_flat.append(instance['Target']['Id'])

        # Get our healthy instances from our autoscaler
        logger.info("Getting healthy instances on our autoscaler")
        as_instances = get_autoscaler_healthy_instances( autoscaling_group_name )

        failures = 0
        for instance in as_instances:
            if instance['InstanceId'] in instance_health_flat:
                logger.info("FAIL - Instance " + instance['InstanceId'] + " from our autoscaler is still in our target group")
                failures = failures + 1
            else:
                logger.info("Success - Instance " + instance['InstanceId'] + " from our autoscaler is not in our target group")

        if failures == 0:
            logger.info("SUCCESS - We have no instances from the autoscaling group on this target group...")
            break
        else:
            logger.info("Found " + str(failures) + " instances still on the target group from the ASG.  Waiting 10 seconds...")

        time.sleep( 10 )



def flatten_instance_health_array_from_loadbalancer( input_instance_array ):
    output = []
    for instance in input_instance_array:
        output.append(instance['InstanceId'])
    return output



def flatten_instance_health_array_from_loadbalancer_only_healthy( input_instance_array ):
    output = []
    for instance in input_instance_array:
        if instance['State'] == 'InService':
            output.append(instance['InstanceId'])

    return output


def wait_for_complete_loadbalancer_autoscaler_attachment( loadbalancer_name, autoscaling_group_name ):
    logger.info("Waiting for attachment of autoscaler " + autoscaling_group_name + " to load balancer:" + loadbalancer_name)

    while True:
        # Get instances from load balancer
        logger.info("Getting load balancer")
        loadbalancer = get_load_balancer(loadbalancer_name)

        # Get instance ids from load balancer
        logger.info("Getting instance ids from load balancer")
        temptwo = get_instance_ids_of_load_balancer(loadbalancer)

        # Get their healths (on the ELB)
        logger.info("Getting instance health on the load balancer")
        instance_health = elb.describe_instance_health(
            LoadBalancerName=loadbalancer_name,
            Instances=loadbalancer['Instances']
        )
        instance_health = instance_health['InstanceStates']

        # Put it into a flat array so we can check "in" it
        instance_health_flat = flatten_instance_health_array_from_loadbalancer_only_healthy(instance_health)

        # Get our healthy instances from our autoscaler
        logger.info("Getting healthy instances on our autoscaler")
        autoscaler = get_autoscaling_group( autoscaling_group_name )
        as_instances = get_autoscaler_healthy_instances( autoscaler )

        successes = 0
        for instance in as_instances:
            if instance['InstanceId'] in instance_health_flat:
                logger.info("SUCCESS - Instance " + instance['InstanceId'] + " is healthy in our ELB")
                successes = successes + 1
            else:
                logger.info("FAIL - Instance " + instance['InstanceId'] + " is unhealthy or not present in our ELB")

        if successes >= len(as_instances):
            if int(autoscaler['DesiredCapacity']) == successes:
                logger.info("We have " + str(successes) + " healthy instances on the elb and on the ASG")
                break
            else:
                logger.info("Found " + str(successes) + " healthy instances on the elb from the ASG " + str(autoscaler['DesiredCapacity']) + " to continue.  Waiting 10 seconds...")
        else:
            logger.info("Found " + str(successes) + " healthy instances on the elb from the ASG " + str(autoscaler['DesiredCapacity']) + " to continue.  Waiting 10 seconds...")

        time.sleep( 10 )

######################
# Core application logic
######################

# Verify/get our load balancer
logger.info("Ensuring that \"" + options.autoscaler + "\" is a valid autoscaler in the current region...")
autoscaler = get_autoscaling_group(options.autoscaler)
if autoscaler is False:
    logger.info("ERROR: '" + options.autoscaler + "' is NOT a valid autoscaler, exiting...")
    parser.print_usage()
    exit(1)

# Grab some variables we need to use/save/reuse below
autoscaler_old_max_size = int(autoscaler['MaxSize'])
autoscaler_old_desired_capacity = int(autoscaler['DesiredCapacity'])

# Check if we need to increase our max size
logger.info("Checking if our current desired size is equal to our max size (if so we have to increase max size to deploy)...")
if autoscaler_old_max_size == autoscaler_old_desired_capacity:
    logger.info("Updating max size of autoscaler by one from " + str(autoscaler_old_max_size))
    if update_auto_scaling_group_max_size(options.autoscaler, (autoscaler_old_max_size + 1) ) is True:
        logger.info("Successfully expanded autoscalers max size temporarily for deployment...")
    else:
        logger.info("Failed expanding max-size, will be unable to deploy (until someone implements a different mechanism to deploy)")
        exit(1)

# Letting the user know what this autoscaler is attached to...
if len(autoscaler['LoadBalancerNames']) > 0:
    logger.info("This autoscaler is attached to the following Elastic Load Balancers (ELBs): ")
    for name in autoscaler['LoadBalancerNames']:
        logger.info("    ELB: " + name)
else:
    logger.info("This autoscaler is not attached to any ELBs")

if len(autoscaler['TargetGroupARNs']) > 0:
    logger.info("This autoscaler is attached to the following Target Groups (for ALBs): ")
    for name in autoscaler['TargetGroupARNs']:
        logger.info("    TG: " + name)
else:
    logger.info("This autoscaler is not attached to any Target Groups")

if (options.force):
    logger.info("ALERT: We are force-deploying so we're going to skip checking for and setting suspended processes...")
    resume_all_processes( options.autoscaler )
else:
    logger.info("Ensuring that we don't have certain suspended processes that we will need to proceed...")
    required_processes = ['Terminate','Launch','HealthCheck','AddToLoadBalancer']
    suspended = get_suspended_processes(autoscaler)
    succeed = True
    for process in required_processes:
        if process in suspended:
            logger.info("Error: This autoscaler currently has the required suspended process: " + process)
            succeed = False
    if succeed == False:
        exit(1)

# Suspending processes so things on an autoscaler can settle
logger.info("Suspending processes so everything can settle on ELB/ALB/TGs: ")
suspend_new_processes = ['ScheduledActions', 'AlarmNotification', 'AZRebalance']
suspend_processes( options.autoscaler, suspend_new_processes )

logger.info("Waiting 3 seconds so the autoscaler can settle from the above change...")
time.sleep(3)

# Get our autoscaler info again... just-incase something changed on it before doing the below health-check logic...
autoscaler = get_autoscaling_group(options.autoscaler)

# Wait to have healthy == desired instances on the autoscaler
logger.info("Ensuring that we have the right number of instances on the autoscaler")
wait_for_autoscaler_to_have_healthy_desired_instances(autoscaler)

# Only if we want to not force-deploy do we check if the instances get health on their respective load balancers/target groups
if (not options.force):
    # Wait to have healthy instances on the load balancers
    if len(autoscaler['LoadBalancerNames']) > 0:
        logger.info("Ensuring that these instances are healthy on the load balancer(s)")
        for name in autoscaler['LoadBalancerNames']:
            logger.info("Waiting for all instances to be healthy in " + name + "...")
            wait_for_complete_loadbalancer_autoscaler_attachment( name, options.autoscaler )

    # Wait to have healthy instances on the target groups
    if len(autoscaler['TargetGroupARNs']) > 0:
        logger.info("Ensuring that these instances are healthy on the target group(s)")
        for name in autoscaler['TargetGroupARNs']:
            logger.info("Waiting for all instances to be healthy in " + name + "...")
            wait_for_complete_targetgroup_autoscaler_attachment( name, options.autoscaler )

logger.info("====================================================")
logger.info("Performing rollout...")
logger.info("====================================================")

# Get our autoscaler info _one_ last time, to make sure we have the instances that we'll be rolling out of service...
autoscaler = get_autoscaling_group(options.autoscaler)

# Gather the instances we need to kill...
instances_to_kill = get_autoscaler_healthy_instances(autoscaler)
if options.checkifinstancesneedtobeterminated:
    logger.info("INFO: Checking if there are instances to skip")
    instances_to_skip = get_instances_to_skip(instances_to_kill, autoscaler)
    for instance in instances_to_skip:
        logger.info("Skiping instance " + instance['InstanceId'])
        instances_to_kill.remove(instance)

# Keep a tally of current instances...
current_instance_list = get_autoscaler_healthy_instances(autoscaler)

def find_aws_instances_in_first_list_but_not_in_second( array_one, array_two ):
    output = []
    for instance_array_one in array_one:
        # logger.info("Found " + instance_array_one['InstanceId'] + " in array one...")
        found = False
        for instance_array_two in array_two:
            if instance_array_two['InstanceId'] == instance_array_one['InstanceId']:
                # logger.info("Found " + instance_array_two['InstanceId'] + " in array two also")
                found = True

        if (not found):
            # logger.info("Did not find instance in array two, returning this...")
            output.append(instance_array_one)

    return output

# Increase our desired size by one so a new instance will be started (usually from a new launch configuration)
# Don't increase desired capacity if there is no instance to kill
if len(instances_to_kill) > 0:
    logger.info("Increasing desired capacity by one from " + str(autoscaler['DesiredCapacity']) + " to " + str(autoscaler['DesiredCapacity'] + 1))
    set_desired_capacity( options.autoscaler, autoscaler['DesiredCapacity'] + 1 )

downscaled = False


for i, instance in enumerate(instances_to_kill):

    # Sleep a little bit every loop, just incase...
    logger.info("Sleeping for 3 seconds so the autoscaler can catch-up...")
    time.sleep(3)

    # This is used in the external "down" helper below, but we need to do this here before we start shutting down this instance
    old_instance_details = describe_instance(instance['InstanceId'])

    # Wait to have healthy == desired instances on the autoscaler
    logger.info("Ensuring that we have the right number of instances on the autoscaler")
    wait_for_autoscaler_to_have_healthy_desired_instances( options.autoscaler )

    # Wait for new instances to spin up...
    while True:
        logger.info("Waiting for new instance(s) to spin up...")
        # Lets figure out what the new instance ID(s) are here...
        new_current_instance_list = get_autoscaler_healthy_instances(options.autoscaler)
        new_instances = find_aws_instances_in_first_list_but_not_in_second(new_current_instance_list, current_instance_list)
        if len(new_instances) == 0:
            logger.info("There are no new instances yet... waiting 10 seconds...")
            time.sleep(10)
        else:
            break;

    # Only if we instructed that we want to not skip the health checks on the way up
    if (not options.skip):
        # Wait to have healthy instances on the load balancers
        if len(autoscaler['LoadBalancerNames']) > 0:
            logger.info("Ensuring that these instances are healthy on the load balancer(s)")
            for name in autoscaler['LoadBalancerNames']:
                logger.info("Waiting for all instances to be healthy in " + name + "...")
                wait_for_complete_loadbalancer_autoscaler_attachment( name, options.autoscaler )

        # Wait to have healthy instances on the target groups
        if len(autoscaler['TargetGroupARNs']) > 0:
            logger.info("Ensuring that these instances are healthy on the target group(s)")
            for name in autoscaler['TargetGroupARNs']:
                logger.info("Waiting for all instances to be healthy in " + name + "...")
                wait_for_complete_targetgroup_autoscaler_attachment( name, options.autoscaler )

    # Wait for instance to get healthy (custom handler) if desired...
    if (options.checkifnewserverisupcommand):
        logger.info("Running external health up check upon request...")
        while True:
            succeeded_health_up_check = True
            # String replacing the instance ID and/or the instance IP address into the external script
            for new_instance in new_instances:
                try:
                    instance_details = describe_instance(new_instance['InstanceId'])
                    private_ip_address = instance_details['PrivateIpAddress']
                    if 'PublicIpAddress' in instance_details:
                        public_ip_address = instance_details['PublicIpAddress']
                        logger.info("Found new instance " + new_instance['InstanceId'] + " with private IP address " + private_ip_address + " and public IP " + public_ip_address)
                    else:
                        logger.info("Found new instance " + new_instance['InstanceId'] + " with private IP address " + private_ip_address + " and NO public IP address")

                    tmpcommand = str(options.checkifnewserverisupcommand)
                    tmpcommand = tmpcommand.replace('NEW_INSTANCE_ID',new_instance['InstanceId'])
                    tmpcommand = tmpcommand.replace('NEW_INSTANCE_PRIVATE_IP_ADDRESS', private_ip_address)
                    if 'PublicIpAddress' in instance_details:
                        tmpcommand = tmpcommand.replace('NEW_INSTANCE_PUBLIC_IP_ADDRESS', public_ip_address)
                    logger.info("Executing external health shell command: " + tmpcommand)
                    retval = call(tmpcommand, shell=True)
                    # print "Got return value " + str(retval)
                    if (retval != 0):
                        succeeded_health_up_check = False
                except:
                    logger.info("WARNING: Failed trying to figure out if new instance is healthy")

            if succeeded_health_up_check:
                logger.info("SUCCESS: We are done checking instances with a custom command")
                break
            else:
                logger.info("FAIL: We are done checking instances with a custom command, but (at least one) has failed, re-trying in 10 seconds...")
            time.sleep(10)

    logger.info("Should de-register instance " + instance['InstanceId'] + " from ALB/ELBs if attached...")

    # If we have load balancers...
    if len(autoscaler['LoadBalancerNames']) > 0:
        for name in autoscaler['LoadBalancerNames']:
            logger.info("De-registering " + instance['InstanceId'] + " from load balancer " + name + "...")
            deregister_instance_from_load_balancer( instance['InstanceId'], name )

    # If we have target groups...
    if len(autoscaler['TargetGroupARNs']) > 0:
        for name in autoscaler['TargetGroupARNs']:
            logger.info("De-registering " + instance['InstanceId'] + " from target group " + name + "...")
            deregister_instance_from_target_group( instance['InstanceId'], name )

    # If we have load balancers...
    if len(autoscaler['LoadBalancerNames']) > 0:
        for name in autoscaler['LoadBalancerNames']:
            while True:
                instance_ids = get_instance_ids_of_load_balancer( name )
                logger.info("Got instance ids...")
                logger.info(instance_ids)
                if instance['InstanceId'] in instance_ids:
                    logger.info("Instance ID is still in load balancer, sleeping for 10 seconds...")
                    time.sleep(10)
                else:
                    logger.info("Instance ID is removed from load balancer, continuing...")
                    break

    # If we have target groups...
    if len(autoscaler['TargetGroupARNs']) > 0:
        for name in autoscaler['TargetGroupARNs']:
            while True:
                instance_ids = get_instance_ids_of_target_group( name )
                if instance['InstanceId'] in instance_ids:
                    logger.info("Instance ID is still in target group, sleeping for 10 seconds...")
                    time.sleep(10)
                else:
                    logger.info("Instance ID is removed from target group, continuing...")
                    break

    # Run a command on server going down, if desired...
    if (options.runbeforeserverdowncommand):
        logger.info("Running external server down command...")
        # String replacing the instance ID and/or the instance IP address into the external script
        old_private_ip_address = old_instance_details['PrivateIpAddress']
        if 'PublicIpAddress' in old_instance_details:
            old_public_ip_address = old_instance_details['PublicIpAddress']

        tmpcommand = str(options.runbeforeserverdowncommand)
        tmpcommand = tmpcommand.replace('OLD_INSTANCE_ID',old_instance_details['InstanceId'])
        tmpcommand = tmpcommand.replace('OLD_INSTANCE_PRIVATE_IP_ADDRESS', old_private_ip_address)
        if 'PublicIpAddress' in old_instance_details:
            tmpcommand = tmpcommand.replace('OLD_INSTANCE_PUBLIC_IP_ADDRESS', old_public_ip_address)
        logger.info("Executing before server down command: " + tmpcommand)
        retval = call(tmpcommand, shell=True)
        # print "Got return value " + str(retval)
        if (retval != 0):
            logger.info("WARNING: Server down command returned retval of " + str(retval))

    # If the user specified they want to wait
    if (options.waitforseconds > 0):
        logger.info("User requested to wait for {0} before terminating instances...".format(options.waitforseconds))
        time.sleep(options.waitforseconds)

    # Re-get our current instance list, for the custom health check script
    time.sleep(2)
    current_instance_list = get_autoscaler_healthy_instances(options.autoscaler)

    # Now terminate our instance in our autoscaling group...
    # If this is our last time in this loop then we want to decrement the capacity along with it
    if (i + 1) == len(instances_to_kill):
        terminate_instance_in_auto_scaling_group( instance['InstanceId'], options.autoscaler, True )
        downscaled = True
    # Otherwise, simply kill this server and wait for it to be replaced, keeping the desired capacity
    else:
        terminate_instance_in_auto_scaling_group( instance['InstanceId'], options.autoscaler )

    # Run a command on server going down, if desired...
    if (options.runafterserverdowncommand):
        logger.info("Running external server down command after...")
        time.sleep(2)
        # String replacing the instance ID and/or the instance IP address into the external script
        old_private_ip_address = old_instance_details['PrivateIpAddress']
        if 'PublicIpAddress' in old_instance_details:
            old_public_ip_address = old_instance_details['PublicIpAddress']

        tmpcommand = str(options.runafterserverdowncommand)
        tmpcommand = tmpcommand.replace('OLD_INSTANCE_ID',old_instance_details['InstanceId'])
        tmpcommand = tmpcommand.replace('OLD_INSTANCE_PRIVATE_IP_ADDRESS', old_private_ip_address)
        if 'PublicIpAddress' in old_instance_details:
            tmpcommand = tmpcommand.replace('OLD_INSTANCE_PUBLIC_IP_ADDRESS', old_public_ip_address)
        logger.info("Executing after server down command: " + tmpcommand)
        retval = call(tmpcommand, shell=True)
        # print "Got return value " + str(retval)
        if (retval != 0):
            logger.info("WARNING: Server down command returned retval of " + str(retval))

instances_to_kill_flat = flatten_instance_health_array_from_loadbalancer( instances_to_kill )

# Before exiting, just incase lets wait for proper detachment of the Classic ELBs (wait for: idle timeout / connection draining to finish)
if (not options.force):
    if len(autoscaler['LoadBalancerNames']) > 0:
        logger.info("Ensuring that these instances are fully detached from the load balancer(s)")
        for name in autoscaler['LoadBalancerNames']:
            logger.info("Waiting for complete detachment of old instances from load balancer '" + name + "'...")
            wait_for_instances_to_detach_from_loadbalancer( instances_to_kill_flat, name )

    # Before exiting, just incase lets wait for proper detachment of the TGs (wait for: idle timeout / connection draining to finish)
    if len(autoscaler['TargetGroupARNs']) > 0:
        logger.info("Ensuring that these instances are fully detached from the target group(s)")
        for name in autoscaler['TargetGroupARNs']:
            logger.info("Waiting for complete detachment of old instances from target group '" + name + "'...")
            wait_for_instances_to_detach_from_target_group( instances_to_kill_flat, name )

# This should never happen unless the above for loop breaks out unexpectedly
if downscaled == False:
    logger.info("Manually decreasing desired capacity back to " + str(autoscaler_old_desired_capacity))
    set_desired_capacity( options.autoscaler, autoscaler_old_desired_capacity )

# Resume our processes...
if (options.force):
    logger.info("ALERT: Resuming all autoscaling processes because of --force...")
    resume_all_processes( options.autoscaler )
else:
    logger.info("Resuming suspended processes...")
    resume_processes(options.autoscaler, suspend_new_processes)

# Check if we need to decrease our max size back to what it was
logger.info("Checking if we changed our max size, if so, shrink it again...")
if autoscaler_old_max_size == autoscaler_old_desired_capacity:
    logger.info("Updating max size of autoscaler down one to " + str(autoscaler_old_max_size))
    if update_auto_scaling_group_max_size(options.autoscaler, autoscaler_old_max_size ) is True:
        logger.info("Successfully shrunk autoscalers max size back to its old value")
    else:
        logger.info("Failed shrinking max-size for some reason")
        exit(1)
else:
    logger.info("Didn't need to shrink our max size")

logger.info("Successfully zero-downtime deployed!")
exit(0)
