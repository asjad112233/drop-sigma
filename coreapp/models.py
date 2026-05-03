from django.db import models


class Store(models.Model):
    name = models.CharField(max_length=255)
    platform = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class Order(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    customer_name = models.CharField(max_length=255)
    total = models.FloatField()
    status = models.CharField(max_length=50)

    def __str__(self):
        return self.customer_name


class Email(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    subject = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=50)

    def __str__(self):
        return self.subject