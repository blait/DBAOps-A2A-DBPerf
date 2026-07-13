output "instance_id" {
  value = aws_instance.prometheus.id
}

output "private_ip" {
  value = aws_instance.prometheus.private_ip
}

output "prometheus_endpoint" {
  value = "http://${aws_instance.prometheus.private_ip}:9090"
}

output "security_group_id" {
  value = aws_security_group.prometheus.id
}
