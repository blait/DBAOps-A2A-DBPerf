output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.this.domain_name}"
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.this.domain_name
}

output "streamlit_repo_url" {
  value = aws_ecr_repository.streamlit.repository_url
}

output "service_name" {
  value = try(aws_ecs_service.streamlit[0].name, null)
}
