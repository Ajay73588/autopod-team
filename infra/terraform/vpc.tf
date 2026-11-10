# AutoPod Cloud -- VPC, subnets, Internet Gateway, public routing.
# ARCHITECTURE.md Section 3.2: one VPC, two public subnets (one per AZ,
# required for the ALB), one IGW, no NAT Gateway / private subnets (an
# explicit cost/complexity trade-off, not an oversight -- see Section 3.2
# and the "Optional Components" list in Section 15).

resource "aws_vpc" "autopod" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "autopod-vpc"
  }
}

resource "aws_internet_gateway" "autopod" {
  vpc_id = aws_vpc.autopod.id

  tags = {
    Name = "autopod-igw"
  }
}

resource "aws_subnet" "public" {
  count                   = length(var.public_subnet_cidrs)
  vpc_id                  = aws_vpc.autopod.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "autopod-public-${var.availability_zones[count.index]}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.autopod.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.autopod.id
  }

  tags = {
    Name = "autopod-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
