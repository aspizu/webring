create table site (
    id serial primary key,
    next int,
    previous int,
    url text not null unique,
    email text not null,
    password_hash text not null,
    created_at int not null,
    valid boolean not null default false,
    foreign key (next) references site (id),
    foreign key (previous) references site (id)
);

create table report (
    id serial primary key,
    site int not null,
    created_at int not null,
    ip_address text not null,
    reason text not null,
    unique (ip_address, site),
    foreign key (site) references site (id)
);

create table status (
    id serial primary key,
    site int not null,
    created_at int not null,
    status text not null,
    foreign key (site) references site (id)
);
