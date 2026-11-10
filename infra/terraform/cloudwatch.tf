# AutoPod Cloud -- CloudWatch CPU alarms wired to the Auto Scaling
# policies. ARCHITECTURE.md Section 10.1/11.4: CloudWatch (not Prometheus)
# drives Auto Scaling, since AWS's own scaling policies are natively wired
# to CloudWatch alarms.

resource "aws_cloudwatch_metric_alarm" "scale_out_cpu_high" {
  alarm_name          = "autopod-cpu-high-scale-out"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Average"
  threshold           = var.scale_out_cpu_threshold
  alarm_description   = "Scale out autopod-app-nodes-asg when average CPU >= ${var.scale_out_cpu_threshold}% for 2 consecutive periods."

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.app_nodes.name
  }

  alarm_actions = [aws_autoscaling_policy.scale_out.arn]
}

resource "aws_cloudwatch_metric_alarm" "scale_in_cpu_low" {
  alarm_name          = "autopod-cpu-low-scale-in"
  comparison_operator = "LessThanOrEqualToThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Average"
  threshold           = var.scale_in_cpu_threshold
  alarm_description   = "Scale in autopod-app-nodes-asg when average CPU <= ${var.scale_in_cpu_threshold}% for 2 consecutive periods."

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.app_nodes.name
  }

  alarm_actions = [aws_autoscaling_policy.scale_in.arn]
}
