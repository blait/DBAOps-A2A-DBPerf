output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  value = [for s in aws_subnet.public : s.id]
}

output "private_subnet_ids" {
  value = [for s in aws_subnet.private : s.id]
}

output "private_route_table_id" {
  value = aws_route_table.private.id
}

output "nat_eni_id" {
  value = try(aws_network_interface.nat[0].id, null)
}

output "vpce_security_group_id" {
  value = try(aws_security_group.vpce[0].id, null)
}
